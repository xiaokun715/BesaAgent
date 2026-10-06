"""工具目录（``tool/catalog.py``，`FR-T-12` / `FR-T-13`）。

**Embedder 由组合根注入**（复用 gateway 的 embedding alias），所以这里塞一个
**确定性的假实现** —— 体检与检索的正确性不该依赖真模型的输出。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest

from tool.base import Tool, ToolContext
from tool.catalog import Catalog, cosine_similarity
from tool.types import ToolResult


class FakeEmbedder:
    """按**标记词**返回固定向量 —— 让相似度可预测。

    真 embedder 的输出会随模型版本变，而「两个工具算不算重复」的**判定逻辑**
    不该跟着它一起变。
    """

    def __init__(self, mapping: Mapping[str, Sequence[float]]) -> None:
        self.mapping = dict(mapping)
        self.calls = 0

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        self.calls += 1
        result = []
        for text in texts:
            vector = [0.0, 0.0]
            for marker, value in self.mapping.items():
                if marker in text:
                    vector = list(value)
                    break
            result.append(vector)
        return result


class ExplodingEmbedder:
    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        raise RuntimeError("embedding 服务挂了")


class Stub(Tool):
    def __init__(self, name: str, description: str, *, category: str = "infra", params=None):
        self.name = name
        self.description = description
        self.category = category
        self.parameters = params or {"type": "object", "properties": {}}

    async def run(self, args: Mapping[str, object], ctx: ToolContext) -> ToolResult:
        return ToolResult(outcome="executed", tool_name=self.name)


def _read_like(name: str, **over) -> Stub:
    return Stub(name, "读取一个文件的内容", **over)


# --------------------------------------------------------------------------- #
# 体检：重复
# --------------------------------------------------------------------------- #


async def test_duplicate_descriptions_are_reported():
    """描述相似度超阈值 → 报「疑似重复」。

    用 embedding 而不是关键词：``read_file`` 与 ``fetch_text`` 关键词一个都不重合，
    但它们在语义上是同一件事。
    """
    embedder = FakeEmbedder({"读取一个文件": [1.0, 0.0]})
    catalog = Catalog(
        tools=[_read_like("read"), _read_like("read_file")],
        embedder=embedder,
        duplicate_threshold=0.9,
    )

    issues = await catalog.inspect()

    assert len(issues) == 1
    assert issues[0].kind == "duplicate"
    assert set(issues[0].tools) == {"read", "read_file"}
    assert "不自动合并" in issues[0].detail


async def test_different_tools_are_not_flagged():
    embedder = FakeEmbedder({"读取一个文件": [1.0, 0.0], "执行命令": [0.0, 1.0]})
    catalog = Catalog(
        tools=[_read_like("read"), Stub("bash", "执行命令")],
        embedder=embedder,
        duplicate_threshold=0.9,
    )
    assert await catalog.inspect() == ()


async def test_inspection_failure_does_not_block_startup(caplog):
    """体检失败**不阻断启动**（与「缺密钥的模型软失败」同一条纪律）。

    但必须出声 —— 静默跳过会让「工具重复」永远查不出来，
    而它的症状是「模型行为不稳定」，离原因很远。
    """
    import logging

    catalog = Catalog(tools=[_read_like("a"), _read_like("b")], embedder=ExplodingEmbedder())
    with caplog.at_level(logging.WARNING, logger="tool.catalog"):
        issues = await catalog.inspect()

    assert issues == ()
    assert any("相似度计算失败" in r.message for r in caplog.records)


async def test_without_an_embedder_only_conflicts_are_checked(caplog):
    import logging

    catalog = Catalog(tools=[_read_like("a")], embedder=None)
    with caplog.at_level(logging.INFO, logger="tool.catalog"):
        assert await catalog.inspect() == ()


# --------------------------------------------------------------------------- #
# 体检：冲突
# --------------------------------------------------------------------------- #


async def test_same_param_with_different_types_is_a_conflict():
    """**名字相近 + 同名参数类型不一致** = 冲突。

    为什么不是「参数名一样就算冲突」：``path`` 在两个工具里都叫 ``path`` 很正常。
    真正会让人（和模型）混的是「名字像同一类工具，但 ``path`` 一个是字符串一个是数组」
    —— 那是**会填错**的地方，而类型不同是模型猜不出来的。
    """
    left = Stub(
        "read", "读",
        params={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    right = Stub(
        "read_batch", "批量读",
        params={"type": "object", "properties": {"path": {"type": "array"}}},
    )

    catalog = Catalog(tools=[left, right], embedder=None)
    issues = await catalog.inspect()

    assert len(issues) == 1
    assert issues[0].kind == "conflict"
    assert "path" in issues[0].detail
    assert "string" in issues[0].detail and "array" in issues[0].detail


async def test_unrelated_names_with_different_types_are_not_a_conflict():
    """名字不像的工具各自用各自的类型，不该被报成冲突。"""
    left = Stub("read", "读", params={"type": "object", "properties": {"path": {"type": "string"}}})
    right = Stub("query", "查", params={"type": "object", "properties": {"path": {"type": "array"}}})

    assert await Catalog(tools=[left, right], embedder=None).inspect() == ()


async def test_same_param_same_type_is_fine():
    """同名同类型不是冲突 —— 那是正常的约定（``path`` 到处都叫 ``path``）。"""
    left = Stub("read", "读", params={"type": "object", "properties": {"path": {"type": "string"}}})
    right = Stub("read_file", "读", params={"type": "object", "properties": {"path": {"type": "string"}}})
    assert await Catalog(tools=[left, right], embedder=None).inspect() == ()


# --------------------------------------------------------------------------- #
# 选择：分类（静态、零成本）
# --------------------------------------------------------------------------- #


async def test_category_selection_narrows_the_pool():
    """分类是**静态的、零成本的**收窄 —— agent 在某个阶段只该看见它那类工具。"""
    catalog = Catalog(
        tools=[
            Stub("read", "读", category="infra"),
            Stub("write_case", "写用例", category="test_case"),
            Stub("run_env", "起环境", category="environment"),
        ],
        selection="category",
    )

    chosen = await catalog.select(categories=["test_case"])
    assert [d.name for d in chosen] == ["write_case"]

    everything = await catalog.select()
    assert len(everything) == 3


async def test_definition_carries_the_category():
    catalog = Catalog(tools=[Stub("read", "读", category="infra")])
    (definition,) = await catalog.select()
    assert definition.category == "infra"


# --------------------------------------------------------------------------- #
# 选择：检索（动态，且必须稳定）
# --------------------------------------------------------------------------- #


async def test_rag_selection_picks_the_relevant_tools():
    embedder = FakeEmbedder({"读文件": [1.0, 0.0], "执行命令": [0.0, 1.0], "登录用例": [1.0, 0.0]})
    catalog = Catalog(
        tools=[
            Stub("read", "读文件"),
            Stub("bash", "执行命令"),
            Stub("case_for_login", "登录用例"),
        ],
        embedder=embedder,
        selection="rag",
        rag_top_k=2,
    )

    chosen = await catalog.select(query="登录用例怎么写")
    assert "case_for_login" in [d.name for d in chosen]
    assert "bash" not in [d.name for d in chosen]


async def test_rag_selection_is_stable_within_a_session():
    """**同一会话内工具列表必须稳定**（`FR-T-13`）。

    不缓存的话，同样的任务在两轮里拿到不同的工具集 —— 模型会不知所措，
    而「为什么这次工具不一样」也解释不了。这与 `D-5`（一期不做语义路由）同一条理由。
    """
    embedder = FakeEmbedder({"读文件": [1.0, 0.0], "执行命令": [0.0, 1.0]})
    catalog = Catalog(
        tools=[Stub("read", "读文件"), Stub("bash", "执行命令")],
        embedder=embedder,
        selection="rag",
        rag_top_k=1,
    )

    first = await catalog.select(query="读个文件", session_id="s-1")
    calls_after_first = embedder.calls
    second = await catalog.select(query="读个文件", session_id="s-1")

    assert [d.name for d in first] == [d.name for d in second]
    assert embedder.calls == calls_after_first, "第二轮应当命中缓存，不再打 embedding"


async def test_rag_falls_back_instead_of_returning_nothing(caplog):
    """检索不可用时**退回上一级**，**绝不返回空集**。

    空工具集会静默地让模型「什么都不能做」，而它看起来像模型不听话 ——
    那是极难归因的一类问题。
    """
    import logging

    catalog = Catalog(
        tools=[Stub("read", "读文件"), Stub("bash", "执行命令")],
        embedder=ExplodingEmbedder(),
        selection="rag",
    )

    with caplog.at_level(logging.WARNING, logger="tool.catalog"):
        chosen = await catalog.select(query="读个文件")

    assert len(chosen) == 2, "检索失败时必须退回，不能返回空集"


async def test_rag_without_an_embedder_warns_at_construction(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="tool.catalog"):
        Catalog(tools=[Stub("read", "读")], embedder=None, selection="rag")
    assert any("没有注入 embedder" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# 相似度本身
# --------------------------------------------------------------------------- #


def test_cosine_similarity_basics():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_similarity_with_an_empty_vector_is_zero():
    """空向量返回 0 而不是抛异常 —— 体检不该因为一个工具没写描述就让整个启动失败。"""
    assert cosine_similarity([], [1.0]) == 0.0
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0
