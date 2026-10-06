"""让工具集**可管理**（`FR-T-12` / `FR-T-13`）。

工具少的时候，「全塞给模型」是对的。**工具一多，同一个做法会同时毁掉两件事**：
上下文被撑爆，以及模型的选择准确率下降（选项越多越容易选错）。

这个文件管的就是「**该给模型看哪些**」，外加一件启动期的事：**工具体检**。

## 两件事的性质完全不同

====================  ============  ==================  ==================================
                     何时           成本                 失败的样子
====================  ============  ==================  ==================================
**体检**（重复/冲突）   启动期一次      一次 embedding       模型在两个几乎一样的工具间随机选
                                                         —— 表现为「行为不稳定」，离原因很远
**选择**（分类/检索）   每次调用        分类零成本；检索一次  给了不该给的（选错）/
                                      向量运算             没给该给的（干不了活）
====================  ============  ==================  ==================================

## 三种选择手段，但**聚合不是第三级叠加**

分类与检索是叠加的（先按类别收窄，再在类别内检索）。
而**聚合**（把 N 个同构工具收成一个带 ``action`` 的入口）是**另一种取舍**：

    并集 schema 表达不了「action=read 时才需要 path」。
    于是模型填错的那一刻**本地拦不住**（schema 通过了），只能等执行时才发现 ——
    而选错工具至少还能靠工具名的语义兜底。

所以它只在动作**高度同构**时用，且**不得与分类/检索混用在同一批工具上** ——
那等于让模型在两个层次上各选一次。本模块不实现聚合（它改变的是工具的**定义方式**，
不是选择策略），但把这条取舍写在这里，免得有人以为「再加一级就好」。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from tool.base import Tool
from tool.types import ToolDefinition

__all__ = [
    "Catalog",
    "CatalogIssue",
    "Embedder",
    "SelectionMode",
    "cosine_similarity",
]

_log = logging.getLogger(__name__)

SelectionMode = Literal["off", "category", "rag"]


class Embedder(Protocol):
    """把文本变成向量。

    **由组合根注入**（复用 gateway 的 embedding alias）——
    ``src/tool`` 不能 import ``gateway``。

    只定义这一个方法，是为了让单测能塞一个确定性的假实现：
    体检与检索的正确性不该依赖真模型的输出。
    """

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


@dataclass(frozen=True)
class CatalogIssue:
    """体检发现的一个问题。

    **只告警，不自动处理**（`FR-T-12`）：两个描述相似的工具可能是
    **有意的差异化实现**（一个 ``read`` 一个 ``tail``），自动合并会把其中一个悄悄吃掉。
    """

    kind: Literal["duplicate", "conflict"]
    tools: tuple[str, ...]
    detail: str

    def __str__(self) -> str:
        return f"[{self.kind}] {', '.join(self.tools)}：{self.detail}"


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """余弦相似度。**空向量返回 0**（而不是抛异常）——
    体检不该因为一个工具没写描述就让整个启动失败。"""
    if not left or not right:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm = (sum(a * a for a in left) ** 0.5) * (sum(b * b for b in right) ** 0.5)
    return dot / norm if norm else 0.0


@dataclass
class Catalog:
    """工具目录：体检 + 选择。

    ``embedder`` 为 ``None`` 时**体检退化成只查冲突**（结构性那部分），
    检索则退回分类。这不是静默降级 —— 构造时会告警说清少了什么。
    """

    tools: Sequence[Tool]
    embedder: Embedder | None = None
    #: 描述相似度超过它就报「疑似重复」
    duplicate_threshold: float = 0.92
    selection: SelectionMode = "category"
    rag_top_k: int = 20
    #: 同一会话内检索结果必须稳定（`FR-T-13`）—— 否则模型会看到工具突然消失
    stable_per_session: bool = True

    #: 缓存的工具描述向量，键是工具名
    _vectors: dict[str, tuple[float, ...]] = field(default_factory=dict, init=False)
    #: 会话内的检索结果缓存，键是 (session_id, 类别集合, query)
    _selected: dict[tuple[str, tuple[str, ...], str], tuple[str, ...]] = field(
        default_factory=dict, init=False
    )

    def __post_init__(self) -> None:
        if self.selection == "rag" and self.embedder is None:
            _log.warning(
                "tool.catalog.selection=rag 但没有注入 embedder —— 检索会退回按类别。"
                "**这不是静默降级**：它会在 doctor 里显示为「检索不可用」。"
            )

    # ---------------------------------------------------------------- 体检
    async def inspect(self) -> tuple[CatalogIssue, ...]:
        """启动期体检。**失败不阻断启动**（与「缺密钥的模型软失败」同一条纪律）。"""
        issues: list[CatalogIssue] = []
        issues.extend(self._check_conflicts())
        issues.extend(await self._check_duplicates())

        for issue in issues:
            _log.warning("工具体检：%s", issue)
        if not issues:
            _log.info("工具体检通过：%d 个工具，未发现重复或冲突", len(self.tools))
        return tuple(issues)

    def _check_conflicts(self) -> list[CatalogIssue]:
        """**冲突**：名字相近、但**同名参数的类型不一致**。

        为什么不是「参数名一样就算冲突」：``path`` 在两个工具里都叫 ``path`` 很正常。
        真正会让人（和模型）混的是「名字看起来像同一类工具，但 ``path``
        在一个里是字符串、在另一个里是数组」—— 那是**会填错**的地方。
        """
        issues: list[CatalogIssue] = []
        seen: list[tuple[str, Mapping[str, Mapping[str, object]]]] = [
            (tool.name, _property_types(tool.parameters)) for tool in self.tools
        ]

        for index, (left_name, left_props) in enumerate(seen):
            for right_name, right_props in seen[index + 1 :]:
                if not _names_are_similar(left_name, right_name):
                    continue
                for param in set(left_props) & set(right_props):
                    if left_props[param] != right_props[param]:
                        issues.append(
                            CatalogIssue(
                                kind="conflict",
                                tools=(left_name, right_name),
                                detail=(
                                    f"名字相近，但参数 {param!r} 的类型不一致："
                                    f"{left_props[param]} vs {right_props[param]}。"
                                    "模型很可能在两者之间填错 —— 类型不同是它猜不出来的。"
                                ),
                            )
                        )
        return issues

    async def _check_duplicates(self) -> list[CatalogIssue]:
        """**重复**：描述文本的语义相似度超阈值。

        用 embedding 而不是关键词：两个工具叫 ``read_file`` 与 ``fetch_text``，
        关键词一个都不重合，但它们在语义上是同一件事。
        """
        if self.embedder is None or len(self.tools) < 2:
            return []

        try:
            texts = [
                f"{tool.name}\n{tool.description}\n{_schema_summary(tool.parameters)}"
                for tool in self.tools
            ]
            vectors = await self.embedder.embed(texts)
        except Exception as exc:  # noqa: BLE001
            # **体检失败不阻断启动**。但要出声 —— 静默跳过会让「工具重复」
            # 这件事永远查不出来，而它的症状是「模型行为不稳定」。
            _log.warning("工具体检的相似度计算失败，本次跳过重复检测：%s", exc)
            return []

        if len(vectors) != len(self.tools):  # pragma: no cover - 上游实现有问题
            _log.warning("embedder 返回的向量数（%d）与工具数（%d）不符，跳过重复检测",
                         len(vectors), len(self.tools))
            return []

        issues: list[CatalogIssue] = []
        for index, tool in enumerate(self.tools):
            self._vectors[tool.name] = tuple(vectors[index])

        for index, left in enumerate(self.tools):
            for right in self.tools[index + 1 :]:
                score = cosine_similarity(self._vectors[left.name], self._vectors[right.name])
                if score >= self.duplicate_threshold:
                    issues.append(
                        CatalogIssue(
                            kind="duplicate",
                            tools=(left.name, right.name),
                            detail=(
                                f"描述相似度 {score:.3f} ≥ {self.duplicate_threshold}，疑似同一件事。"
                                "**不自动合并** —— 它们可能是有意的差异化实现"
                                "（如 read 与 tail），合并会把其中一个悄悄吃掉。"
                            ),
                        )
                    )
        return issues

    # ---------------------------------------------------------------- 选择
    async def select(
        self,
        *,
        categories: Iterable[str] | None = None,
        query: str = "",
        session_id: str = "",
    ) -> tuple[ToolDefinition, ...]:
        """挑出「这一次该给模型看」的工具定义。

        Args:
            categories: 只要这些类别（``None`` = 不限）。**这是零成本的收窄**，
                应当优先使用 —— agent 在某个测试阶段只该看见它那类工具。
            query: 任务描述，``selection="rag"`` 时用来检索。
            session_id: 会话标识。检索结果按它缓存，保证同一会话内稳定。
        """
        wanted = tuple(sorted(set(categories or ())))
        pool = [tool for tool in self.tools if not wanted or tool.category in wanted]

        if self.selection != "rag" or not query or self.embedder is None:
            return _definitions(pool)

        names = await self._retrieve(pool, query=query, session_id=session_id, wanted=wanted)
        if not names:
            # **绝不返回空集**（`FR-T-13`）：空工具集会静默地让模型
            # 「什么都不能做」，而它看起来像模型不听话。
            _log.warning("工具检索没有命中任何工具，退回按类别的结果")
            return _definitions(pool)

        chosen = {name for name in names}
        return _definitions([tool for tool in pool if tool.name in chosen])

    async def _retrieve(
        self,
        pool: Sequence[Tool],
        *,
        query: str,
        session_id: str,
        wanted: tuple[str, ...],
    ) -> tuple[str, ...]:
        cache_key = (session_id if self.stable_per_session else "", wanted, query)
        cached = self._selected.get(cache_key)
        if cached is not None:
            # **同一会话内必须稳定**（`FR-T-13`）：不缓存的话，同样的任务
            # 在两轮里拿到不同的工具集 —— 模型会不知所措，而「为什么这次不一样」也解释不了。
            return cached

        try:
            missing = [tool.name for tool in pool if tool.name not in self._vectors]
            if missing:
                texts = [
                    f"{tool.name}\n{tool.description}\n{_schema_summary(tool.parameters)}"
                    for tool in pool
                    if tool.name in missing
                ]
                vectors = await self.embedder.embed(texts)  # type: ignore[union-attr]
                for name, vector in zip(missing, vectors):
                    self._vectors[name] = tuple(vector)

            (query_vector,) = await self.embedder.embed([query])  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            _log.warning("工具检索失败，退回按类别：%s", exc)
            return ()

        scored = sorted(
            pool,
            key=lambda tool: cosine_similarity(query_vector, self._vectors.get(tool.name, ())),
            reverse=True,
        )
        names = tuple(tool.name for tool in scored[: self.rag_top_k])
        self._selected[cache_key] = names
        return names


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def _definitions(tools: Iterable[Tool]) -> tuple[ToolDefinition, ...]:
    return tuple(tool.definition() for tool in tools)


def _property_types(schema: Mapping[str, object]) -> dict[str, str]:
    """从 JSON Schema 里抽出「参数名 → 类型」。用于冲突检测。"""
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return {}
    result: dict[str, str] = {}
    for name, spec in properties.items():
        declared = spec.get("type") if isinstance(spec, Mapping) else None
        result[str(name)] = str(declared if declared is not None else "?")
    return result


def _schema_summary(schema: Mapping[str, object]) -> str:
    """把 schema 压成一行，喂给 embedding。

    带上参数而不是只比描述：两个描述都很含糊的工具
    （「读取内容」与「获取数据」），**参数形状**往往才是它们像不像的关键。
    """
    types = _property_types(schema)
    return " ".join(f"{name}:{kind}" for name, kind in sorted(types.items()))


def _names_are_similar(left: str, right: str) -> bool:
    """名字是否像一个族里的。

    用**公共前缀**而不是编辑距离：``read`` 与 ``read_file`` 是一个族，
    而 ``read`` 与 ``spread`` 编辑距离很近却毫无关系 —— 前缀更贴近「谁会被混淆」。
    """
    if left == right or not left or not right:
        return left == right
    common = 0
    for a, b in zip(left, right):
        if a != b:
            break
        common += 1
    return common >= 3 or left.startswith(right) or right.startswith(left)
