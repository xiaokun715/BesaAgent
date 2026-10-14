"""离线评测：用**数据**回答"哪个组合更好"（`FR-ST-18`）。

**为什么必须有它**：选择可以是聪明的，但**判断哪个组合更好必须靠数据**。
让 LLM 直接说「知识问答用这套更好」，等于把回归测试的基准换成了一次采样
（需求 §2.3 的最后一条）。

**它自己不执行任务**（`NFR-ST-02` / `MST-13`）：执行是 ``src/agent`` 的事，
所以这里**注入一个 runner**（与 ``ChatModel`` / ``Embedder`` 的注入方式同构）。
好处有三条：

- 本层保持"不执行"的边界 —— 发现它在发请求 = 边界破了；
- 单测可以喂一个假 runner，**不需要真的跑 agent**；
- "怎么跑一次任务"的知识留在组合根，将来换执行方式不必改这里。

**它也不落库**：``EvalRecord`` 由调用方交给 ``src/repo``（`STQ-4` 倾向新表）。
只把结果打日志等于没做 —— 所以返回值是**结构化的记录**，不是一行文本。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from foundation.clock import Clock, SystemClock

from strategy.types import AgentProfile, EvalRecord

__all__ = ["BenchmarkTask", "Evaluator", "RunOutcomeLike", "Runner"]

_log = logging.getLogger(__name__)


class RunOutcomeLike(Protocol):
    """一次基准运行的**最小形状**。

    刻意用 ``Protocol`` 而不是 import ``agent.RunOutcome`` ——
    本层不许 import 任何组件模块（`NFR-ST-01`）。形状对得上就行，
    适配由组合根做（和 ``ChatModel`` 之于 ``Gateway.chat`` 一样）。
    """

    @property
    def ok(self) -> bool: ...

    @property
    def elapsed_s(self) -> float: ...

    @property
    def cost(self) -> Any: ...


@dataclass(frozen=True)
class BenchmarkTask:
    """一条基准任务。``payload`` 是给 runner 的输入（本层不解释它）。"""

    name: str
    payload: Mapping[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.payload is None:
            object.__setattr__(self, "payload", {})


#: 跑一次基准任务。**由组合根注入** —— 本层不认识 agent，也不认识模型。
Runner = Callable[[AgentProfile, BenchmarkTask], Awaitable[RunOutcomeLike]]

#: 把一次运行折算成 ``[0,1]`` 的质量分。默认是"成功算 1、失败算 0"。
QualityFn = Callable[[AgentProfile, BenchmarkTask, RunOutcomeLike | None], float]


def _default_quality(
    profile: AgentProfile, task: BenchmarkTask, outcome: RunOutcomeLike | None
) -> float:
    return 1.0 if outcome is not None and outcome.ok else 0.0


class Evaluator:
    """在基准任务集上比较候选组合。**一次运行不阻塞在线**（它是后台任务）。"""

    __slots__ = ("_runner", "_clock", "_quality")

    def __init__(
        self,
        runner: Runner,
        *,
        clock: Clock | None = None,
        quality_of: QualityFn | None = None,
    ) -> None:
        self._runner = runner
        self._clock = clock or SystemClock()
        self._quality = quality_of or _default_quality

    async def evaluate(
        self,
        profiles: Sequence[AgentProfile],
        tasks: Sequence[BenchmarkTask],
        *,
        benchmark_set: str = "default",
        max_combos: int | None = None,
    ) -> tuple[EvalRecord, ...]:
        """评一遍。**返回记录，不落库**。

        Args:
            max_combos: 组合数上界（`C-ST-5`）。``None`` 用调用方给的候选数 ——
                但**调用方必须给一个上界**，因为枚举出来的是几十到几百个，
                全评一遍能跑到天亮。

        ⚠ **单个组合跑失败不影响其它组合**：一次基准跑挂掉只让那条记录难看，
        不该让整轮评测中断（与"记账失败不拖垮调用"同一条纪律）。
        """
        if not profiles or not tasks:
            return ()

        limited = list(profiles)
        truncated = 0
        if max_combos is not None and len(limited) > max_combos:
            truncated = len(limited) - max_combos
            limited = limited[:max_combos]
            _log.info(
                "一次评测最多评 %d 个组合，本次从 %d 个里取了前 %d 个（截掉 %d 个）。"
                "**未评测的组合不代表更差** —— 只是这一轮没轮到它们。",
                max_combos, len(profiles), max_combos, truncated,
            )

        records: list[EvalRecord] = []
        for profile in limited:
            record = await self._evaluate_one(
                profile, tasks, benchmark_set=benchmark_set
            )
            records.append(record)
        return tuple(records)

    async def _evaluate_one(
        self, profile: AgentProfile, tasks: Sequence[BenchmarkTask], *, benchmark_set: str
    ) -> EvalRecord:
        quality_total = 0.0
        cost_total = 0.0
        latency_total = 0.0
        samples = 0

        for task in tasks:
            outcome: RunOutcomeLike | None = None
            try:
                outcome = await self._runner(profile, task)
            except Exception as exc:  # noqa: BLE001 — 一条跑挂不该中断整轮
                _log.warning(
                    "基准任务 %s 在组合 %s 上执行失败（按未成功计入）：%s",
                    task.name, profile.name, exc,
                )
            samples += 1
            quality_total += self._quality(profile, task, outcome)
            if outcome is not None:
                cost_total += float(getattr(outcome, "cost", 0.0) or 0.0)
                latency_total += float(getattr(outcome, "elapsed_s", 0.0) or 0.0) * 1000.0

        return EvalRecord(
            profile_key=profile.profile_key,
            versions=dict(profile.versions),
            quality=(quality_total / samples) if samples else 0.0,
            cost=(cost_total / samples) if samples else 0.0,
            latency=(latency_total / samples) if samples else 0.0,
            samples=samples,
            benchmark_set=benchmark_set,
            at=self._clock.now(),
        )
