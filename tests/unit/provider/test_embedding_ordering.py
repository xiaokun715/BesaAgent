"""向量化响应的**顺序**：``index`` 可信与不可信两种形态。

**这个文件存在的理由**（真实厂商实测踩到的坑，不是假设）：
SiliconFlow 的 ``Qwen/Qwen3-VL-Embedding-8B`` 在批量 ≥9 条时，响应里的 ``index``
会按 **8 条分片重置** —— 9 条返回 ``[0..7, 0]``、12 条返回 ``[0..7, 0..3]``，
而**数组顺序本身是正确的**。

原先的实现一律 ``sorted(key=index)``：稳定排序遇到重复 key 会把后者提到前面，
于是一份**本来正确**的响应被重排成 ``[e0, e8, e1, ...]`` —— 且因为条数没变，
``len(vectors) != len(texts)`` 的自检也拦不住，**完全静默**。

这些用例锁定的是「**宁可相信数组顺序，也不相信一个不构成排列的 index**」这条判据。
纯 mock 传输，零网络（``FR-P-13``）。
"""

from __future__ import annotations

import logging

import httpx
import pytest

from provider.errors import ProtocolError

#: 向量化必须**完整声明**能力（列表 = 替换厂商默认），否则会继承 chat 的能力集
EMB_CFG: dict[str, object] = {
    "model": "Qwen/Qwen3-VL-Embedding-8B",
    "capabilities": ["embedding"],
}

TEXTS = [f"第 {i} 条文本" for i in range(12)]


def _vec(k: int) -> list[float]:
    """第 ``k`` 条**真实文本**的向量。用它把「顺序」变成可断言的事实。"""
    return [float(k), float(k) + 0.5]


def _item(k: int, index: int | None) -> dict[str, object]:
    """数组中的一项：向量对应真实文本 ``k``，``index`` 是上游声称的下标。"""
    item: dict[str, object] = {"embedding": _vec(k)}
    if index is not None:
        item["index"] = index
    return item


def _handler(data: list[dict[str, object]]):
    def _handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"model": "emb", "data": data, "usage": {"prompt_tokens": 7}},
        )

    return _handle


def _identity(n: int) -> tuple[tuple[float, ...], ...]:
    """顺序正确时，``embed`` 应当返回的向量序列。"""
    return tuple(tuple(_vec(k)) for k in range(n))


# --------------------------------------------------------------------------- #
# 一、index 可信：重排仍然生效（原有的防护不能被这次修复削弱）
# --------------------------------------------------------------------------- #


async def test_out_of_order_index_is_still_reordered(make_openai):
    """index 是 ``0..n-1`` 的完整排列时，按 index 重排 —— 原防护必须保留。"""
    # 数组顺序是 [2, 0, 1]，且每项都正确标注了自己的 index
    items = [_item(2, 2), _item(0, 0), _item(1, 1)]
    provider = make_openai(_handler(items), cfg=EMB_CFG)

    result = await provider.embedding_model().embed(TEXTS[:3])

    assert result.vectors == _identity(3), "数组顺序是乱的，但 index 正确 → 必须重排回来"


# --------------------------------------------------------------------------- #
# 二、index 缺失：静默退回数组顺序（上游没给这项信息）
# --------------------------------------------------------------------------- #


async def test_missing_index_falls_back_to_array_order(make_openai, caplog):
    """无 index 字段 → 数组顺序即权威，**不告警**（这是合法形态，不是异常）。"""
    items = [_item(k, None) for k in range(4)]
    provider = make_openai(_handler(items), cfg=EMB_CFG)

    with caplog.at_level(logging.WARNING, logger="provider.openai.embedding"):
        result = await provider.embedding_model().embed(TEXTS[:4])

    assert result.vectors == _identity(4)
    assert not caplog.records, "缺 index 是常态，不该产生告警"


# --------------------------------------------------------------------------- #
# 三、index 不可信：告警并退回数组顺序（本次修复的核心回归）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("n", "indices", "note"),
    [
        (9, [*range(8), 0], "SiliconFlow 实测：9 条 → [0..7, 0]"),
        (12, [*range(8), 0, 1, 2, 3], "SiliconFlow 实测：12 条 → [0..7, 0..3]"),
        (3, [0, 0, 0], "极端：全部重复"),
        (3, [0, 1, 9], "越界下标"),
    ],
)
async def test_untrusted_index_falls_back_and_warns(make_openai, caplog, n, indices, note):
    """index 不是 ``0..n-1`` 的完整排列 → **退回数组顺序**并告警。

    关键在于：数组顺序是对的，而按 index 重排会得到 ``[e0, e8, e1, ...]`` 这种
    看似合理、实则错位的结果 —— 不会抛异常，只会让检索悄悄变差。
    """
    items = [_item(k, indices[k]) for k in range(n)]
    provider = make_openai(_handler(items), cfg=EMB_CFG)

    with caplog.at_level(logging.WARNING, logger="provider.openai.embedding"):
        result = await provider.embedding_model().embed(TEXTS[:n])

    assert result.vectors == _identity(n), f"应退回数组顺序（{note}）"
    assert any("0..n-1" in record.message for record in caplog.records), "必须留下告警"


async def test_fragmented_index_regression_directly(make_openai):
    """把 SiliconFlow 的真实形态钉成一条**不依赖告警**的断言。

    9 条、数组顺序正确、第 9 条 index 是 0。修复前的实现会返回
    ``[v0, v8, v1, v2, ...]`` —— 这里逐位断言它不成立。
    """
    items = [_item(k, 0 if k == 8 else k) for k in range(9)]
    provider = make_openai(_handler(items), cfg=EMB_CFG)

    result = await provider.embedding_model().embed(TEXTS[:9])

    assert len(result.vectors) == 9
    assert result.vectors == _identity(9)
    # 明确否定「修复前」的那个错位结果，让回归意图一眼可见
    assert result.vectors[1] != tuple(_vec(8))


# --------------------------------------------------------------------------- #
# 四、其余自检：条数与维度（FR-P-07）
# --------------------------------------------------------------------------- #


async def test_vector_count_mismatch_is_rejected(make_openai):
    """返回条数少于请求条数 → 报错。少一条会让后续所有向量错位（``FR-P-07``）。"""
    items = [_item(k, k) for k in range(2)]
    provider = make_openai(_handler(items), cfg=EMB_CFG)

    with pytest.raises(ProtocolError) as excinfo:
        await provider.embedding_model().embed(TEXTS[:3])

    assert "不符" in str(excinfo.value)


async def test_dimension_mismatch_is_rejected(make_openai):
    """声明的 dimension 与实际返回不符 → 报错（它与向量库的 ``vector(N)`` 对齐）。"""
    items = [_item(k, k) for k in range(2)]
    provider = make_openai(_handler(items), cfg=dict(EMB_CFG, dimension=3))

    with pytest.raises(ProtocolError) as excinfo:
        await provider.embedding_model().embed(TEXTS[:2])

    assert "dimension=3" in str(excinfo.value)
    assert "实际返回 2" in str(excinfo.value)
