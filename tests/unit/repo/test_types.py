"""``src/repo/types.py`` 的测试。

这些载体都很小，但它们各自挡住一个**静默**问题，所以值得逐条测：

- ``TimeRange`` 的起点晚于终点 → 不校验的话查询会返回空，而调用方会以为「没有数据」；
- ``BatchResult.dropped`` → 丢掉的数据必须能被读到（gateway 的 ``dropped`` 就是反例）；
- ``VectorSpace.dim`` → 维度是空间身份的一部分，不是可选的元数据。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from repo.types import BatchResult, Page, TimeRange, VectorSpace


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #


def test_page_defaults_are_empty_and_exhausted():
    """默认页是空的、并且**明确表示到底了** —— 而不是让调用方去猜。"""
    page: Page[int] = Page()
    assert len(page) == 0
    assert page.has_more is False
    assert page.next_cursor is None


def test_page_is_iterable_so_callers_can_for_over_it():
    page = Page(items=(1, 2, 3), has_more=True, next_cursor="c")
    assert [x for x in page] == [1, 2, 3]


def test_page_is_immutable():
    """frozen 是有意的：分页结果被下游改一半，是最难查的一类 bug。"""
    page = Page(items=(1,))
    with pytest.raises(Exception):
        page.items = (2,)  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# TimeRange
# --------------------------------------------------------------------------- #


def test_time_range_accepts_open_ended_either_side():
    now = datetime(2026, 9, 28, 12, 0, 0)
    assert TimeRange().start is None and TimeRange().end is None
    assert TimeRange(start=now).end is None
    assert TimeRange(end=now).start is None


def test_time_range_accepts_instantaneous_range():
    """起点等于终点是合法的（半开区间下它表示「空」，而不是错误）。"""
    now = datetime(2026, 9, 28)
    assert TimeRange(start=now, end=now).start == now


def test_time_range_rejects_reversed_bounds():
    """起点晚于终点必须**报错** —— 不报的话查询返回空，而调用方以为「没有数据」。"""
    now = datetime(2026, 9, 28)
    with pytest.raises(ValueError, match="起点晚于终点"):
        TimeRange(start=now + timedelta(days=1), end=now)


# --------------------------------------------------------------------------- #
# BatchResult —— 「丢弃必须可见」
# --------------------------------------------------------------------------- #


def test_batch_result_defaults_to_nothing_dropped():
    result = BatchResult(written=10)
    assert result.dropped == 0
    assert result.total == 10


def test_batch_result_makes_drops_readable():
    """丢了多少必须能被读到。

    这是 gateway ``UsageLedger.dropped`` 的反面教材：它记了丢弃数，
    但全仓库没有一处读它 —— 于是「丢了数据」这件事没有任何出口，
    报表上看起来一切正常。
    """
    result = BatchResult(written=997, dropped=3)
    assert result.total == 1000
    assert result.dropped == 3, "丢弃数必须能被调用方读到，才能被记录或告警"


# --------------------------------------------------------------------------- #
# VectorSpace
# --------------------------------------------------------------------------- #


def test_vector_space_identity_includes_dim_and_metric():
    """空间的身份由 (名字, 维度, 度量) 共同决定 —— 三者一起才决定「两个向量能不能比」。"""
    space = VectorSpace(name="memory", dim=1024, metric="cosine", model_key="emb-x")
    assert space.dim == 1024
    assert space.metric == "cosine"
    assert "1024" in str(space) and "cosine" in str(space)


def test_vector_space_defaults_to_cosine():
    assert VectorSpace(name="s", dim=768).metric == "cosine"


@pytest.mark.parametrize("dim", [0, -1, -1024])
def test_vector_space_rejects_non_positive_dim(dim: int):
    """维度必须为正 —— 0 维或负维算出来的距离没有意义，而且不会报错。"""
    with pytest.raises(ValueError, match="维度必须为正"):
        VectorSpace(name="s", dim=dim)


@pytest.mark.parametrize("name", ["", "   "])
def test_vector_space_rejects_blank_name(name: str):
    with pytest.raises(ValueError, match="必须有名字"):
        VectorSpace(name=name, dim=768)
