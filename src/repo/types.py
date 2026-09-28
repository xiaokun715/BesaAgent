"""``src/repo`` 的类型载体。

**放什么**：跨仓储复用的、与存储后端无关的载体 —— 分页、时间范围、批量结果、向量空间描述。

**不放什么**：

- 具体实体（会话、消息、事件…）—— 它们各自有自己的 ORM 模型，不在这里再定义一份；
- 任何 SQLAlchemy 的东西 —— 本模块是**存储无关**的载体，不 import 存储层。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Generic, Literal, TypeVar

__all__ = [
    "BatchResult",
    "Metric",
    "Page",
    "TimeRange",
    "VectorSpace",
]

T = TypeVar("T")

#: 向量距离度量。写成字面量而不是 ``str``，是为了让「写错一个词」在类型检查期就暴露。
Metric = Literal["cosine", "l2", "inner_product"]


@dataclass(frozen=True)
class Page(Generic[T]):
    """一页结果。

    **`next_cursor` 为空表示到底了**，而不是「这一页恰好满了」——
    让调用方去比较「返回条数 == 页大小」来决定要不要继续，会在数据恰好整页时
    多打一次空查询，而且那个判断在**每处调用点**都要重写一遍。
    """

    items: tuple[T, ...] = ()
    #: 下一页的游标；``None`` = 没有下一页
    next_cursor: str | None = None
    #: 是否还有更多。与 ``next_cursor`` 同时给出是为了让「有没有下一页」不用靠解析游标
    has_more: bool = False

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self):
        return iter(self.items)


@dataclass(frozen=True)
class TimeRange:
    """时间范围，**半开区间** ``[start, end)``。

    半开而不是闭区间，是为了让「上一页的末尾」与「下一页的开头」不重叠 ——
    闭区间在分页时会把边界那条记录返回两次，而调用方通常不会去重。

    两端都可以为 ``None``（= 不限）。
    """

    start: datetime | None = None
    end: datetime | None = None

    def __post_init__(self) -> None:
        if self.start is not None and self.end is not None and self.start > self.end:
            raise ValueError(
                f"时间范围的起点晚于终点：start={self.start} end={self.end}；"
                "半开区间要求 start <= end"
            )


@dataclass(frozen=True)
class BatchResult:
    """批量写入的结果。

    **``dropped`` 必须存在，且必须能被读到**。这不是装饰 ——
    gateway 的 ``UsageLedger.dropped`` 就是一个反例：它记了丢弃数，
    但**全仓库没有任何地方读它**，于是「丢了数据」这件事没有任何出口。
    本模块不重复同一个错误：批量写入只要可能丢，就必须回报丢了多少。
    """

    written: int = 0
    #: 因上限/去重/冲突等原因被丢弃的条数。**不为 0 时调用方必须记录或告警**
    dropped: int = 0

    @property
    def total(self) -> int:
        return self.written + self.dropped


@dataclass(frozen=True)
class VectorSpace:
    """一个向量空间：**(模型键, 维度, 度量)** 三者共同定义。

    这三点一起才决定「两个向量能不能比」—— 维度不同的向量算出来的距离
    **没有任何数学意义**，而多数向量库不会为此报错。所以维度是空间身份的一部分，
    不是一个可选的元数据。

    ``dim`` 在写入与检索时都必须与数据实际维度一致，不一致**必须报错**
    （与 provider 的 ``FR-P-07`` 同一条纪律）。
    """

    name: str
    dim: int
    metric: Metric = "cosine"
    model_key: str = ""
    #: 该空间的额外索引参数（如 HNSW 的 m / ef_construction），由存储实现解释
    index_options: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("向量空间必须有名字")
        if self.dim <= 0:
            raise ValueError(f"向量维度必须为正，得到 {self.dim}")

    def __str__(self) -> str:
        return f"{self.name}(dim={self.dim}, {self.metric})"
