"""预算：**从模型窗口算起**，按比例分给各段，不够时按固定优先级砍。

三件事按顺序发生，顺序不能反（`MC-12`）：

```
① available_input = model_window − reserve_output − safety_margin
② 先扣掉「不可动的两段」：system + 当前输入
③ 剩下的按比例分给 window / transition / session_summary / long_term
```

**第②步排在第③步之前是刻意的**：反过来的结果是「预算紧的时候先把任务本身砍了」——
模型拿到一堆历史，却不知道自己要做什么。

**窗口从哪来**：由 gateway 按 alias 提供（`DC-1` / `MC-11`）。
本模块**不自己配一个窗口** —— 那样一换 alias 就与真实模型脱节。
候选都没声明时，使用方必须**显式配**一个兜底值并**出声**（见 ``fallback_model_window``）。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

from context.types import BudgetPlan, Segment
from gateway.tokens import estimate_text_tokens

__all__ = ["BudgetPolicy", "allocate_blocks", "estimate_tokens"]

_log = logging.getLogger(__name__)

#: 估算口径来自 gateway（`DC-2`：全平台**只有一份**）。
estimate_tokens = estimate_text_tokens

#: 可分配的四段（`system` / `current` 不在其中 —— 它们不可动）。
DYNAMIC_SEGMENTS: tuple[Segment, ...] = ("window", "transition", "session_summary", "long_term")

_DEFAULT_SEGMENTS: Mapping[str, float] = {
    "window": 0.50,
    "transition": 0.15,
    "session_summary": 0.20,
    "long_term": 0.15,
}

#: 各段下限。**低于下限就整段不注入**，而不是注入半截（`FR-C-13`）——
#: 半截摘要比没有摘要更误导：模型会以为那就是全部。
_DEFAULT_FLOORS: Mapping[str, int] = {
    "window": 256,
    "transition": 64,
    "session_summary": 64,
    "long_term": 64,
}


@dataclass(frozen=True)
class BudgetPolicy:
    """预算参数（``context.budget`` 段）。"""

    reserve_output_tokens: int = 4096
    safety_margin_tokens: int = 1024
    #: 模型没声明 ``context_window`` 时的兜底值。
    #:
    #: ``None`` 表示**拒绝兜底** —— 那时会用 ``model_window`` 为 0 报出"预算不足"，
    #: 而不是悄悄按一个大数字算（悄悄按大数字算的下场是超窗被厂商拒）。
    fallback_model_window: int | None = 32_768
    segments: Mapping[str, float] = field(default_factory=lambda: dict(_DEFAULT_SEGMENTS))
    floors: Mapping[str, int] = field(default_factory=lambda: dict(_DEFAULT_FLOORS))

    @classmethod
    def from_config(cls, cfg: Mapping[str, object] | None) -> "BudgetPolicy":
        data = dict(cfg or {})
        raw_segments = data.get("segments")
        segments = dict(_DEFAULT_SEGMENTS)
        if isinstance(raw_segments, Mapping):
            for key, value in raw_segments.items():
                if key in DYNAMIC_SEGMENTS:
                    segments[str(key)] = float(value)  # type: ignore[arg-type]

        raw_floors = data.get("floors")
        floors = dict(_DEFAULT_FLOORS)
        if isinstance(raw_floors, Mapping):
            for key, value in raw_floors.items():
                if key in DYNAMIC_SEGMENTS:
                    floors[str(key)] = int(value)  # type: ignore[arg-type]

        fallback = data.get("fallback_model_window", 32_768)
        policy = cls(
            reserve_output_tokens=int(data.get("reserve_output_tokens", 4_096)),
            safety_margin_tokens=int(data.get("safety_margin_tokens", 1_024)),
            fallback_model_window=None if fallback is None else int(fallback),  # type: ignore[arg-type]
            segments=segments,
            floors=floors,
        )
        policy._validate()
        return policy

    def _validate(self) -> None:
        total = sum(self.segments.values())
        if total > 1.0 + 1e-9:
            # `C-C-6`：配置错误在**启动期**暴露。配错了不会报错，
            # 只会让"可用输入"超过真实容量 —— 症状是超窗被厂商拒。
            raise ValueError(
                f"context.budget.segments 之和必须 ≤ 1.0，得到 {total:.3f}"
                f"（{' + '.join(f'{k}={v}' for k, v in self.segments.items())}）"
            )
        if self.reserve_output_tokens < 0 or self.safety_margin_tokens < 0:
            raise ValueError("context.budget 的 reserve_output_tokens / safety_margin_tokens 不能为负")
        if self.fallback_model_window is not None and self.fallback_model_window <= 0:
            raise ValueError("context.budget.fallback_model_window 必须为正或 null")

    def resolve_window(self, alias: str, *, from_gateway: int | None) -> int:
        """定出这次要按多大的窗口算。

        ``from_gateway`` 为 ``None``（候选都没声明）时用兜底值**并打日志** ——
        兜底本身是可以的，**悄悄兜底不行**。
        """
        if from_gateway and from_gateway > 0:
            return from_gateway
        if self.fallback_model_window is None:
            _log.warning(
                "alias %r 的候选模型都没有声明 context_window，且未配置兜底 —— "
                "本次按 0 处理，会报「预算不足」而不是猜一个数字。"
                "请在 models.<key> 里补 context_window。",
                alias,
            )
            return 0
        _log.warning(
            "alias %r 的候选模型都没有声明 context_window，按兜底值 %d 计算。"
            "**请补上 models.<key>.context_window** —— 兜底值与真实窗口不符时，"
            "症状是超窗被厂商拒，而本地看不出原因。",
            alias, self.fallback_model_window,
        )
        return self.fallback_model_window

    def plan(self, *, model_window: int, demands: Mapping[Segment, int]) -> BudgetPlan:
        """算配额并做取舍。``demands`` 是各段**想要多少**。

        Returns:
            :class:`BudgetPlan`，其中 ``allocated`` 是给各段的上限（供上层裁剪块），
            ``used`` / ``dropped`` 是实际结果，``insufficient`` 表示连不可动的两段都放不下。
        """
        available = max(0, model_window - self.reserve_output_tokens - self.safety_margin_tokens)
        notes: list[str] = []

        fixed_demand = int(demands.get("system", 0)) + int(demands.get("current", 0))
        insufficient = fixed_demand > available
        if insufficient:
            notes.append(
                f"system + 当前输入就要 {fixed_demand} token，超过可用输入 {available} —— "
                "**不会截断它们**（它们承载任务本身），请换更大的窗口或缩短输入"
            )

        remaining = max(0, available - fixed_demand)

        # ③ 按比例分。**先按"实际会用到的量"夹一次** ——
        # 只按比例算的话，没人要的段会白白扣着配额（比如 transition 分到 160
        # 却没有任何过渡区内容），而想要更多的段拿不到 —— 那部分容量就这么浪费了。
        allocated: dict[str, int] = {
            seg: min(
                int(remaining * self.segments.get(seg, 0.0)),
                int(demands.get(seg, 0)),
            )
            for seg in DYNAMIC_SEGMENTS
        }

        # 还有剩 → 按**优先级从高到低**分给还没满足的段（`DYNAMIC_SEGMENTS` 即优先级顺序）
        leftover = remaining - sum(allocated.values())
        for seg in DYNAMIC_SEGMENTS:
            if leftover <= 0:
                break
            need = int(demands.get(seg, 0)) - allocated[seg]
            if need > 0:
                give = min(need, leftover)
                allocated[seg] += give
                leftover -= give

        used: dict[Segment, int] = {"system": int(demands.get("system", 0)),
                                    "current": int(demands.get("current", 0))}
        dropped: dict[Segment, int] = {"system": 0, "current": 0}

        for seg in DYNAMIC_SEGMENTS:
            demand = int(demands.get(seg, 0))
            quota = allocated[seg]
            floor = self.floors.get(seg, 0)
            # 下限约束的是「**被迫截断**」这件事，不是「这一段天然就小」：
            #
            # - demand=900 而只分到 200（< 下限 500）→ 只能塞进去一个被切碎的开头，
            #   那比不给更误导 → **整段不注入**；
            # - demand=30（一条短记忆）而分到 30 → 它本来就小，**完整地给出去**。
            #
            # 少了 `demand > quota` 这个前提，一条短记忆会被这条规则误杀 ——
            # 而症状是「记忆明明有，就是不进上下文」，极难查。
            if demand > quota and quota < floor:
                used[seg] = 0
                dropped[seg] = demand
                notes.append(f"{seg} 只分到 {quota} < 下限 {floor}，整段不注入")
            else:
                used[seg] = min(demand, quota)
                dropped[seg] = max(0, demand - quota)

        return BudgetPlan(
            model_window=model_window,
            available_input=available,
            reserve_output=self.reserve_output_tokens,
            safety_margin=self.safety_margin_tokens,
            allocated={seg: allocated.get(seg, 0) for seg in DYNAMIC_SEGMENTS},
            used=used,
            dropped={seg: value for seg, value in dropped.items() if value},
            insufficient=insufficient,
            notes=tuple(notes),
        )


def allocate_blocks(blocks, *, quota: int) -> tuple[list, list]:
    """在一段内按配额裁剪块。

    **整块保留或整块丢弃，不切半个**（`FR-C-13`）：半截摘要比没有摘要更误导。
    块的顺序即重要性顺序（越靠前越重要），由上层排好。

    Returns:
        ``(kept, dropped)`` —— dropped 是**丢掉的块**，调用方要把它记进账本。
    """
    kept: list = []
    dropped: list = []
    used = 0
    for block in blocks:
        cost = estimate_tokens(block.content)
        if used + cost <= quota:
            kept.append(block)
            used += cost
        else:
            dropped.append(block)
    return kept, dropped
