"""向量层里**不需要数据库**的那部分：列类型、解析、维度白名单、绑定方式。

**为什么这组值得写**：向量检索里最贵的一次错误是「绑定方式写错」——
它**只有在真的连上 pgvector 时**才暴露，而字面量手测又会通过（见设计文档 §4.2）。
所以这里用一条**盯着 SQL 文本与绑定类型**的断言把它钉住：
哪怕将来有人"顺手"把数组绑定改成字符串，这里会先红。
"""

from __future__ import annotations

import pytest
from sqlalchemy.dialects.postgresql import ARRAY

from apps.server.storage.postgres.vector import (
    EMBEDDING_TABLES,
    SUPPORTED_DIMS,
    DimensionMismatch,
    PgVectorStore,
    UnknownSpace,
    UnsupportedDimension,
    Vector,
    VectorSpaceRow,
    _table_name,
    _upsert_sql,
    build_vector_store,
    format_vector,
    open_vector_store,
    parse_vector,
    spaces_from_config,
)


# ---------------------------------------------------------------- 列类型
def test_col_spec_emits_pgvector_type():
    assert Vector(1024).get_col_spec() == "vector(1024)"
    assert Vector(768).get_col_spec() == "vector(768)"


def test_vector_dim_must_be_positive():
    with pytest.raises(ValueError):
        Vector(0)


def test_parse_vector_accepts_string_and_sequence():
    assert parse_vector("[1,2,3]") == (1.0, 2.0, 3.0)
    assert parse_vector([1, 2, 3]) == (1.0, 2.0, 3.0)
    assert parse_vector("[]") == ()
    assert parse_vector(None) is None


def test_format_vector_round_trips():
    values = (0.5, -1.25, 3.0)
    assert parse_vector(format_vector(values)) == values


# ---------------------------------------------------------------- 维度白名单
def test_supported_dims_exclude_oversized_vectors():
    """`4096` **不在**支持列表里：pgvector 的 HNSW 上限是 2000 维。

    建一张索引建不出来的表，得到的是「检索悄悄变慢」——
    比一个响亮的失败难查得多。
    """
    assert all(dim <= 2000 for dim in SUPPORTED_DIMS)
    assert set(EMBEDDING_TABLES) == set(SUPPORTED_DIMS)


def test_table_name_routes_by_dimension():
    assert _table_name(1024) == "embeddings_1024"
    assert _table_name(768) == "embeddings_768"


@pytest.mark.parametrize("dim", [4096, 384, 1536])
def test_unsupported_dimension_is_rejected(dim):
    with pytest.raises(UnsupportedDimension):
        _table_name(dim)


def test_oversized_dimension_error_names_the_pgvector_limit():
    """报错要说清**为什么不行**与**怎么办** —— 只说「不支持」会让人去翻源码。"""
    with pytest.raises(UnsupportedDimension) as excinfo:
        _table_name(4096)
    message = str(excinfo.value)
    assert "2000" in message
    assert "halfvec" in message


# ---------------------------------------------------------------- 绑定方式
def test_upsert_binds_the_vector_as_an_array_and_casts_it():
    """**这是本模块最容易写错的一处**（设计文档 §4.2）。

    实测：pgvector 定义了 ``double precision[] → vector``，**没有** ``text → vector``。
    于是「把向量格式化成字符串传进去」这种最自然的写法，在字面量下能跑、
    在参数化查询下必失败。这条断言把正确写法钉住。
    """
    statement = _upsert_sql("embeddings_1024")
    sql = str(statement)

    assert "CAST(:embedding AS vector)" in sql, "写入必须显式 cast 成 vector"
    assert "ON CONFLICT (space, owner_kind, owner_id)" in sql, "upsert 的锚是 owner 唯一约束"

    bound = statement._bindparams["embedding"]  # noqa: SLF001 — 就是为了盯住绑定类型
    assert isinstance(bound.type, ARRAY), "参数必须声明成数组类型（text 绑不过去）"


# ---------------------------------------------------------------- 配置
def test_spaces_from_config_single():
    spaces = spaces_from_config({"space": "default", "dim": 1024, "metric": "cosine", "model_key": "m"})
    assert len(spaces) == 1
    assert (spaces[0].name, spaces[0].dim, spaces[0].metric) == ("default", 1024, "cosine")


def test_spaces_from_config_list_form():
    spaces = spaces_from_config(
        {"spaces": [{"name": "a", "dim": 768}, {"name": "b", "dim": 1024, "metric": "l2"}]}
    )
    assert [(s.name, s.dim, s.metric) for s in spaces] == [("a", 768, "cosine"), ("b", 1024, "l2")]


def test_spaces_from_config_defaults():
    spaces = spaces_from_config({})
    assert spaces[0].name == "default"
    assert spaces[0].metric == "cosine"


# ---------------------------------------------------------------- 装配与错误
async def test_open_vector_store_returns_none_without_database():
    """没有数据库时返回 ``None`` —— 记忆会退到结构化检索（`DV-9`），不阻断。"""
    assert await open_vector_store(None, {"space": "default", "dim": 1024}) is None
    assert build_vector_store(None, {}) is None


async def test_unknown_space_is_rejected_with_the_known_list():
    store = PgVectorStore(None, spaces=())
    with pytest.raises(UnknownSpace) as excinfo:
        await store._resolve(None, "nope")  # noqa: SLF001
    assert "没有登记" in str(excinfo.value)


def test_exceptions_have_a_common_base():
    from apps.server.storage.postgres.vector import VectorError

    for exc in (UnsupportedDimension, UnknownSpace, DimensionMismatch):
        assert issubclass(exc, VectorError)


def test_vector_space_row_is_registered_on_the_shared_base():
    """向量表必须注册到**同一个** ``foundation/db.Base`` —— 这是同事务的前提。"""
    from foundation.db import Base

    assert "vector_spaces" in Base.metadata.tables
    assert "embeddings_1024" in Base.metadata.tables
    assert VectorSpaceRow.__table__.name == "vector_spaces"
