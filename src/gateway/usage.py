"""token 用量记账。

**gateway 不写数据库**（``NFR-G-06``）。本模块在内存中聚合，由 :meth:`UsageLedger.drain`
批量交给 ``src/repo`` —— 结构上等于「gateway 产生数据，组合根把它接到 repo 上」，
而不是「gateway 依赖 repo」。

**交付方向不能反过来**：一旦 gateway import 了 repo，它就绑死在某种存储上，
而用量记账是**最不该失败**的东西（它失败会让账单对不上），
不该因为数据库不可用而拖垮模型调用。

**``None`` 与 ``0`` 的区别贯穿本模块**：``Usage.input_tokens is None`` 表示
「上游没说」，不是「用了 0 个 token」。见 ``provider/types.py`` 的 ``Usage``。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from gateway.types import Cost
from provider.types import Usage

__all__ = ["UsageLedger", "UsageRecord"]


@dataclass(frozen=True)
class UsageRecord:
    """一次模型调用的用量记录。"""

    trace_id: str = ""
    alias: str = ""
    model_key: str = ""
    provider: str = ""
    model: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    #: 归属维度：一次会话、一个 agent、一个用户 —— 「跑一轮全量回归花了多少钱」靠它聚合
    session_id: str | None = None
    caller: str | None = None
    #: 单调时钟读数。**不是绝对时间**：本记录产生于一次调用内部，
    #: 需要绝对时间戳时由交付方在落库时补。
    mono_at: float = 0.0
    cost: Cost = field(default_factory=Cost)
    #: 是否发生了降级 —— 「这次特别贵」往往就是因为降级到了强模型
    degraded: bool = False
    #: 本次是第几次尝试（含首发）。重试会重复计费，这个字段让账单可解释。
    attempt_index: int = 0

    @property
    def usage(self) -> Usage:
        return Usage(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cached_input_tokens=self.cached_input_tokens,
        )

    @classmethod
    def from_parts(
        cls,
        *,
        usage: Usage,
        cost: Cost,
        **extra: Any,
    ) -> UsageRecord:
        return cls(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            cost=cost,
            **extra,
        )


class UsageLedger:
    """内存用量账本。

    **有界**（``max_records``）是刻意的：一个长跑的 agent 进程会产生海量记录，
    如果交付方（repo）挂了或变慢，无界的账本会**先吃掉内存**，
    把一个「记账失败」升级成「进程 OOM」。

    溢出时丢弃**最旧**的记录并计数告警 —— 丢旧账比崩进程好，
    而且 ``dropped`` 让「账单少了几笔」这件事**可见**，而不是静默丢失。
    """

    def __init__(self, *, max_records: int = 10_000) -> None:
        if max_records < 1:
            raise ValueError("max_records 必须 >= 1")
        self._max = max_records
        self._records: list[UsageRecord] = []
        self._dropped = 0

    # ---------------------------------------------------------------- 写
    def record(self, item: UsageRecord) -> None:
        self._records.append(item)
        if len(self._records) > self._max:
            overflow = len(self._records) - self._max
            del self._records[:overflow]
            self._dropped += overflow

    def drain(self) -> tuple[UsageRecord, ...]:
        """取出并清空 —— 交付给 ``src/repo`` 的唯一入口。"""
        drained = tuple(self._records)
        self._records.clear()
        return drained

    # ---------------------------------------------------------------- 读
    @property
    def records(self) -> tuple[UsageRecord, ...]:
        return tuple(self._records)

    @property
    def dropped(self) -> int:
        """被丢弃的记录数。**大于 0 就是缺陷**，应当在监控上报警。"""
        return self._dropped

    def totals(
        self,
        *,
        session_id: str | None = None,
        alias: str | None = None,
        caller: str | None = None,
    ) -> Usage:
        """按维度汇总用量。

        聚合时 ``None`` **不参与求和**（既不是 0 也不让整个结果变未知）：
        10 次调用里 9 次有数据、1 次上游没返回，结果应该是那 9 次的和，
        而不是「未知」—— 后者会让整个报表失去意义。
        """
        total = Usage()
        for item in self._records:
            if session_id is not None and item.session_id != session_id:
                continue
            if alias is not None and item.alias != alias:
                continue
            if caller is not None and item.caller != caller:
                continue
            total = total + item.usage
        return total

    def cost_by_model(self) -> dict[str, Cost]:
        """按模型汇总成本。价格未知的模型**单独保留 ``amount=None``**，
        不混进 0 —— 否则「总成本」会凭空少一块且看不出来。"""
        from decimal import Decimal

        buckets: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"amount": None, "currency": ""}
        )
        for item in self._records:
            bucket = buckets[item.model_key]
            bucket["currency"] = bucket["currency"] or item.cost.currency
            if item.cost.amount is None:
                continue
            current = bucket["amount"] or Decimal(0)
            bucket["amount"] = current + item.cost.amount

        return {
            key: Cost(currency=str(value["currency"]), amount=value["amount"])
            for key, value in buckets.items()
        }
