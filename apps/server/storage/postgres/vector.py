"""pgvector 的实现 —— `vector(N)` 列、HNSW 索引、`<=>` 距离检索（`FR-R-08`）。

**本模块只实现协议，不定义协议**（`NFR-S-01`）：`VectorStore` / `VectorSpace` /
`VectorRecord` / `VectorHit` 都在 ``src/repo/embedding.py``。这里做的是
「在 Postgres 上把它们真的跑起来」。

设计细节见 ``docs/vector/架构概要设计-vector.md``。三处最容易写错的地方，
在这里先说明白：

1. **绑定必须走数组 cast**（§4.2）。pgvector 定义了 ``double precision[] → vector``
   的 assignment cast，**没有** ``text → vector``。而「把向量格式化成字符串传进去」
   是最自然的写法，它在字面量下能跑、在参数化查询下必失败 —— 实测确认过。
   所以这里一律 ``CAST(:v AS vector)`` 且参数声明为 ``ARRAY(Float)``。
2. **维度不匹配是硬错误**（`DV-7`）。截断或补齐后的距离算得出来、排得出序，
   但没有数学意义，而且**不会报错**。
3. **空间必须已登记**（`DV-6`）。未登记就自动创建的话，空间的维度会来自
   「第一条数据」—— 而那条数据本身可能就是错的。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    bindparam,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import UserDefinedType

from foundation.db import Base, BigIntPk
from repo.embedding import VectorHit, VectorRecord
from repo.types import VectorSpace

__all__ = [
    "EMBEDDING_TABLES",
    "SUPPORTED_DIMS",
    "DimensionMismatch",
    "PgVectorStore",
    "UnknownSpace",
    "UnsupportedDimension",
    "Vector",
    "VectorSpaceRow",
    "build_vector_store",
    "open_vector_store",
    "spaces_from_config",
]

_log = logging.getLogger(__name__)

#: 支持的维度。**白名单，不是动态建表**（`DV-2`）：
#: 出现新维度要改代码 + 加迁移，而不是让运行时有建表权。
#:
#: ⚠ **只有 ≤ 2000 维能上 HNSW** —— 这是 pgvector 的硬限制，实测报错原文：
#: ``column cannot have more than 2000 dimensions for hnsw index``。
#: 所以 4096 **不在这里**：支持它需要改成 ``halfvec``（上限 4000）或降维，
#: 那是一次真正的设计变更，不是加一行白名单。**列进"支持"就意味着"建得出索引"** ——
#: 否则会得到一张只能全表扫描的表，而症状是"检索变慢了"，离原因很远。
SUPPORTED_DIMS: tuple[int, ...] = (768, 1024)

#: 度量 → pgvector 的距离运算符。
#:
#: ``<#>`` 返回的是**负内积**，所以三种度量都是「升序 = 最相近」——
#: 这点很容易在 ``<#>`` 上写反（按降序排会得到最不相似的）。
_OPERATORS: Mapping[str, str] = {"cosine": "<=>", "l2": "<->", "inner_product": "<#>"}

#: 索引的 ops 类。当前只建 cosine 索引（`DV-4`）。
_COSINE_OPS = "vector_cosine_ops"

_HNSW_WITH = {"m": 16, "ef_construction": 64}

#: pgvector 的 HNSW 索引维度上限（实测：超过它建索引会直接报错）。
_HNSW_MAX_DIMS = 2000


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #


class VectorError(Exception):
    """向量层的基类异常。"""


class UnsupportedDimension(VectorError):
    """维度不在白名单里。**不动态建表**（`DV-2`）。"""


class UnknownSpace(VectorError):
    """空间没有登记（`DV-6`）。"""


class DimensionMismatch(VectorError):
    """维度不匹配（`DV-7`）。**这是本模块最该拦住的一种错**：它不会自己暴露。"""


# --------------------------------------------------------------------------- #
# 列类型
# --------------------------------------------------------------------------- #


class Vector(UserDefinedType):
    """pgvector 的 ``vector(N)`` 列类型。

    **为什么自己写而不是用 ``pgvector`` 这个包**（`DV-3`）：需要的只有三件事 ——
    建表时吐 ``vector(N)``、读回来时解析、写进去时让参数能转成向量。
    前两件各是几行，第三件靠 ``CAST(:v AS vector)``（见模块 docstring），
    而这个包会往 ``postgres`` 这个可选依赖组里再加一个依赖。

    ``bind_processor`` **有意不实现**：写入一律走 ``CAST(:embedding AS vector)``，
    参数由 ``ARRAY(Float)`` 声明。若让本类型去处理绑定，就得在 SQLAlchemy 与
    asyncpg 之间再搭一层编解码，而那条路正是 ``text → vector`` 不存在的死路。
    """

    cache_ok = True

    def __init__(self, dim: int) -> None:
        if dim <= 0:
            raise ValueError(f"向量维度必须为正，得到 {dim}")
        self.dim = dim

    def get_col_spec(self, **kw: Any) -> str:
        return f"vector({self.dim})"

    def result_processor(self, dialect: Any, coltype: Any):  # noqa: ANN201
        def process(value: Any) -> tuple[float, ...] | None:
            return parse_vector(value)

        return process

    def literal_processor(self, dialect: Any):  # noqa: ANN201
        # 只用于「看一眼会生成什么 SQL」（离线 autogenerate）。**运行时不走这里** ——
        # 运行时一律绑定参数，不把一千个浮点拼进 SQL。
        def process(value: Any) -> str:
            return "'" + format_vector(value) + "'"

        return process

    def __repr__(self) -> str:
        return f"Vector({self.dim})"


def format_vector(values: Sequence[float]) -> str:
    """把向量格式化成 pgvector 的文本形态 ``[1,2,3]``。

    **只在字面量场景用**（离线的 ``literal_processor``、以及测试的断言）。
    运行时的写入走绑定参数。
    """
    return "[" + ",".join(repr(float(x)) for x in values) + "]"


def parse_vector(value: Any) -> tuple[float, ...] | None:
    """把从库里读回来的向量解析成元组。

    asyncpg 拿回来的可能是 ``'[1,2,3]'`` 字符串，也可能是已经解析好的列表
    （取决于驱动与是否注册过 codec）—— 两种都要能处理。
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return tuple(float(x) for x in value)
    text_value = str(value).strip()
    if not text_value:
        return None
    if text_value.startswith("[") and text_value.endswith("]"):
        text_value = text_value[1:-1]
    if not text_value:
        return ()
    return tuple(float(part) for part in text_value.split(","))


# --------------------------------------------------------------------------- #
# 表模型
# --------------------------------------------------------------------------- #


class VectorSpaceRow(Base):
    """向量空间的登记表。**运行时的唯一事实来源**（§2.2）。"""

    __tablename__ = "vector_spaces"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    space: Mapped[str] = mapped_column(Text, nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    metric: Mapped[str] = mapped_column(String(16), default="cosine", nullable=False)
    #: 最后写进这个空间的向量是用哪个模型算的。**用于模型换代时的排查** ——
    #: 空间名不等于模型键：同一个空间可以被不同别名写入，只要维度一致。
    model_key: Mapped[str] = mapped_column(Text, default="", nullable=False)
    index_options: Mapped[Mapping[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (UniqueConstraint("space", name="uq_vector_spaces_space"),)

    def __repr__(self) -> str:
        return f"VectorSpaceRow(space={self.space!r}, dim={self.dim}, {self.metric})"


class _EmbeddingColumns:
    """按维度分表时，各表**完全一致的那些列**。"""

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    space: Mapped[str] = mapped_column(Text, nullable=False)
    owner_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_id: Mapped[str] = mapped_column(Text, nullable=False)
    model_key: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def _embedding_table_args(table: str) -> tuple[Any, ...]:
    return (
        UniqueConstraint("space", "owner_kind", "owner_id", name=f"uq_{table}_owner"),
        Index(
            f"ix_{table}_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": _COSINE_OPS},
            postgresql_with=dict(_HNSW_WITH),
        ),
    )


class Embedding768(_EmbeddingColumns, Base):
    """768 维向量（``models.emb-mock``）。"""

    __tablename__ = "embeddings_768"
    embedding: Mapped[Any] = mapped_column(Vector(768), nullable=False)
    __table_args__ = _embedding_table_args("embeddings_768")


class Embedding1024(_EmbeddingColumns, Base):
    """1024 维向量（``models.emb-dashscope``）。"""

    __tablename__ = "embeddings_1024"
    embedding: Mapped[Any] = mapped_column(Vector(1024), nullable=False)
    __table_args__ = _embedding_table_args("embeddings_1024")


#: 维度 → 表。**表数 = 维度数**，不随实体数增长（§2.1）。
EMBEDDING_TABLES: Mapping[int, type[Base]] = {
    768: Embedding768,
    1024: Embedding1024,
}


def _table_name(dim: int) -> str:
    if dim not in EMBEDDING_TABLES:
        hint = ""
        if dim > _HNSW_MAX_DIMS:
            hint = (
                f"\n⚠ {dim} 维**超过 pgvector 的 HNSW 上限**（{_HNSW_MAX_DIMS} 维）："
                "建索引会直接报 `column cannot have more than 2000 dimensions for hnsw index`。\n"
                "要支持它得改用 `halfvec`（上限 4000）或先降维 —— 那是一次设计变更，"
                "不是加一行白名单。"
            )
        raise UnsupportedDimension(
            f"不支持 {dim} 维向量。已支持：{', '.join(str(d) for d in SUPPORTED_DIMS)}。\n"
            "新增维度需要改 SUPPORTED_DIMS 并加一个迁移 —— **不由运行时动态建表**："
            "那会把「谁能建表」从迁移手里漏出去，症状是「这张表是谁建的」永远查不出来。"
            + hint
        )
    return f"embeddings_{dim}"


# --------------------------------------------------------------------------- #
# 实现
# --------------------------------------------------------------------------- #


@dataclass
class _Resolved:
    space: VectorSpace
    table: str
    operator: str


class PgVectorStore:
    """``VectorStore`` 协议在 pgvector 上的实现。

    **无状态（除了一个空间缓存）**：每个方法都收 ``tx``，所以「这次读写属于哪个事务」
    一眼可见 —— 这是「向量与业务行同事务」（`FR-R-08`）能成立的形式。
    """

    __slots__ = ("_database", "_spaces", "_loaded")

    def __init__(self, database: Any | None = None, *, spaces: Iterable[VectorSpace] = ()) -> None:
        self._database = database
        self._spaces: dict[str, VectorSpace] = {s.name: s for s in spaces}
        self._loaded = False

    # ---------------------------------------------------------------- 空间
    @property
    def spaces(self) -> Mapping[str, VectorSpace]:
        return dict(self._spaces)

    async def ensure_spaces(self, spaces: Sequence[VectorSpace] | None = None) -> None:
        """把配置里的空间**播种并校验**到 ``vector_spaces``。

        已存在但维度不同 —— **报错**，不是覆盖。覆盖会让已有数据全部失效，
        而失效的表现是「检索结果变得驴唇不对马嘴」，比启动失败难查得多。
        """
        if spaces:
            for space in spaces:
                self._spaces[space.name] = space

        if self._database is None:
            return

        async with self._database.transaction() as tx:
            for space in self._spaces.values():
                _table_name(space.dim)  # 提前把「不支持的维度」挡在写库之前
                await tx.execute(
                    text(
                        "INSERT INTO vector_spaces (space, dim, metric, model_key, index_options, created_at)"
                        " VALUES (:space, :dim, :metric, :model_key, '{}', :created_at)"
                        " ON CONFLICT (space) DO NOTHING"
                    ),
                    {
                        "space": space.name,
                        "dim": space.dim,
                        "metric": space.metric,
                        "model_key": space.model_key,
                        "created_at": datetime.now(timezone.utc),
                    },
                )

            rows = await tx.fetch_all(select(VectorSpaceRow))
            for row in rows:
                declared = self._spaces.get(row.space)
                if declared is None:
                    # 库里有一个配置里没有的空间。**不删** —— 它可能有数据；
                    # 但要说出来，否则「为什么这个空间一直在」没人答得上来。
                    _log.warning(
                        "vector_spaces 里的 %r（dim=%d）不在配置中 —— 它不会被使用，也不会被删除",
                        row.space, row.dim,
                    )
                    continue
                if row.dim != declared.dim or row.metric != declared.metric:
                    raise DimensionMismatch(
                        f"向量空间 {row.space!r} 在库里是 (dim={row.dim}, metric={row.metric})，"
                        f"而配置是 (dim={declared.dim}, metric={declared.metric})。\n"
                        "**不会自动覆盖**：覆盖会让已有向量全部失效，而失效的表现是"
                        "「检索结果驴唇不对马嘴」，远比启动失败难查。\n"
                        "请确认维度变更后重建向量数据，或改回原配置。"
                    )
            self._loaded = True

    async def _resolve(self, tx: Any, space: str) -> _Resolved:
        """把空间名解析成 ``(维度, 表, 运算符)``。"""
        if not self._loaded and self._database is not None:
            rows = await tx.fetch_all(select(VectorSpaceRow))
            for row in rows:
                self._spaces.setdefault(
                    row.space,
                    VectorSpace(
                        name=row.space, dim=row.dim, metric=row.metric, model_key=row.model_key
                    ),
                )
            self._loaded = True

        known = self._spaces.get(space)
        if known is None:
            raise UnknownSpace(
                f"向量空间 {space!r} 没有登记。已知空间：{', '.join(sorted(self._spaces)) or '（无）'}。\n"
                "空间必须在启动时由 ensure_spaces() 播种（`DV-6`）—— "
                "**不自动创建**：那会让空间的维度来自第一条数据，而那条数据本身可能就是错的。"
            )
        return _Resolved(
            space=known,
            table=_table_name(known.dim),
            operator=_OPERATORS.get(known.metric, _OPERATORS["cosine"]),
        )

    # ---------------------------------------------------------------- 写
    async def upsert(self, tx: Any, records: Sequence[VectorRecord]) -> int:
        """写入或替换向量，返回写入条数。**与调用方在同一个事务**。"""
        if not records:
            return 0

        grouped: dict[tuple[str, int, str, str], list[dict[str, Any]]] = {}
        for record in records:
            resolved = await self._resolve(tx, record.space)
            values = tuple(float(x) for x in record.embedding)
            if len(values) != resolved.space.dim:
                raise DimensionMismatch(
                    f"向量空间 {record.space!r} 是 {resolved.space.dim} 维，"
                    f"而这条向量是 {len(values)} 维（owner={record.owner_kind}:{record.owner_id}）。\n"
                    "**不截断、不补齐**：那样算出来的距离没有数学意义，而且不会报错。"
                )
            key = (resolved.table, resolved.space.dim, resolved.operator, record.owner_kind)
            grouped.setdefault(key, []).append(
                {
                    "space": record.space,
                    "owner_kind": record.owner_kind,
                    "owner_id": record.owner_id,
                    "model_key": record.model_key,
                    "embedding": list(values),
                    "created_at": datetime.now(timezone.utc),
                }
            )

        written = 0
        for (table, _dim, _op, _kind), rows in grouped.items():
            statement = _upsert_sql(table)
            await tx.execute(statement, rows)  # executemany：一次往返多行
            written += len(rows)
        return written

    async def delete(
        self, tx: Any, *, space: str, owner_kind: str, owner_ids: Sequence[str]
    ) -> int:
        """删除向量。**硬删除记忆时必须连它一起删**（`FR-M-03`）。"""
        if not owner_ids:
            return 0
        resolved = await self._resolve(tx, space)
        statement = text(
            f"DELETE FROM {resolved.table}"
            " WHERE space = :space AND owner_kind = :owner_kind AND owner_id IN :ids"
        ).bindparams(bindparam("ids", expanding=True))
        result = await tx.execute(
            statement,
            {"space": space, "owner_kind": owner_kind, "ids": list(owner_ids)},
        )
        return int(result.rowcount or 0)

    # ---------------------------------------------------------------- 读
    async def search(
        self,
        tx: Any,
        *,
        space: str,
        vector: Sequence[float],
        top_k: int,
        owner_kind: str | None = None,
        owner_ids: Sequence[str] | None = None,
    ) -> tuple[VectorHit, ...]:
        """最近邻检索。**返回距离，不返回向量本身** —— 调用方只要排序与相关度。"""
        if top_k <= 0:
            return ()
        resolved = await self._resolve(tx, space)

        query = tuple(float(x) for x in vector)
        if len(query) != resolved.space.dim:
            raise DimensionMismatch(
                f"查询向量是 {len(query)} 维，而空间 {space!r} 是 {resolved.space.dim} 维。\n"
                "**不截断、不补齐** —— 维度不同的向量算出来的距离没有任何数学意义。"
            )

        conditions = ["space = :space"]
        params: dict[str, Any] = {
            "space": space,
            "query": list(query),
            "top_k": int(top_k),
        }
        if owner_kind is not None:
            conditions.append("owner_kind = :owner_kind")
            params["owner_kind"] = owner_kind
        if owner_ids:
            conditions.append("owner_id IN :ids")
            params["ids"] = list(owner_ids)

        distance = f"embedding {resolved.operator} CAST(:query AS vector)"
        statement = text(
            f"SELECT owner_kind, owner_id, {distance} AS distance"
            f" FROM {resolved.table}"
            f" WHERE {' AND '.join(conditions)}"
            f" ORDER BY {distance}"
            " LIMIT :top_k"
        ).bindparams(bindparam("query", type_=ARRAY(Float)))
        if owner_ids:
            statement = statement.bindparams(bindparam("ids", expanding=True))

        # ⚠ 用 ``execute`` 而不是 ``fetch_all``：后者只返回**第一列**
        # （``foundation/database.py`` 的 docstring 写明了），
        # 而这里要的是三列 —— 用错的表现是「owner_kind 拿到了，owner_id 是 None」。
        result = await tx.execute(statement, params)
        hits: list[VectorHit] = []
        for row in result.mappings().all():
            hits.append(
                VectorHit(
                    owner_kind=str(row["owner_kind"]),
                    owner_id=str(row["owner_id"]),
                    distance=float(row["distance"]),
                )
            )
        return tuple(hits)


def _upsert_sql(table: str) -> Any:
    """构造 upsert 语句。

    ``CAST(:embedding AS vector)`` 的参数类型在 :func:`upsert` 的调用处以
    ``ARRAY(Float)`` 声明（``executemany`` 的参数列表是普通 dict，
    所以类型写在语句这一侧）。
    """
    return text(
        f"INSERT INTO {table} (space, owner_kind, owner_id, model_key, embedding, created_at)"
        " VALUES (:space, :owner_kind, :owner_id, :model_key, CAST(:embedding AS vector), :created_at)"
        " ON CONFLICT (space, owner_kind, owner_id)"
        " DO UPDATE SET embedding = EXCLUDED.embedding,"
        "               model_key = EXCLUDED.model_key,"
        "               created_at = EXCLUDED.created_at"
    ).bindparams(bindparam("embedding", type_=ARRAY(Float)))


# --------------------------------------------------------------------------- #
# 装配
# --------------------------------------------------------------------------- #

#: ``repo.embedding`` 段的默认值。
_DEFAULT_SPACE = "default"
_DEFAULT_DIM = 1024
_DEFAULT_METRIC = "cosine"


def spaces_from_config(cfg: Mapping[str, Any] | None) -> list[VectorSpace]:
    """从 ``repo.embedding`` 段构造空间列表。

    两种写法：

    - 简单：``{space: default, dim: 1024, metric: cosine, model_key: emb-dashscope}``
    - 多个：``{spaces: [{name: default, dim: 1024}, ...]}``

    **维度必须显式配**（`VQ-1`）：别名的候选会换（``emb-mock`` ↔ ``emb-dashscope``），
    而空间维度**不该随环境漂移** —— 漂移的那一刻，已有向量全部失效。
    """
    data = dict(cfg or {})
    raw = data.get("spaces")
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) and raw:
        spaces: list[VectorSpace] = []
        for entry in raw:
            if not isinstance(entry, Mapping):
                raise ValueError(f"repo.embedding.spaces 的每一项必须是映射，得到 {type(entry).__name__}")
            spaces.append(
                VectorSpace(
                    name=str(entry.get("name") or _DEFAULT_SPACE),
                    dim=int(entry.get("dim") or _DEFAULT_DIM),
                    metric=str(entry.get("metric") or _DEFAULT_METRIC),  # type: ignore[arg-type]
                    model_key=str(entry.get("model_key") or ""),
                )
            )
        return spaces

    return [
        VectorSpace(
            name=str(data.get("space") or _DEFAULT_SPACE),
            dim=int(data.get("dim") or _DEFAULT_DIM),
            metric=str(data.get("metric") or _DEFAULT_METRIC),  # type: ignore[arg-type]
            model_key=str(data.get("model_key") or ""),
        )
    ]


def build_vector_store(
    database: Any | None, cfg: Mapping[str, Any] | None
) -> PgVectorStore | None:
    """按配置建 store。**没有数据库时返回 ``None``** —— 记忆会退到结构化检索（`DV-9`）。"""
    if database is None:
        return None
    return PgVectorStore(database, spaces=spaces_from_config(cfg))


async def open_vector_store(
    database: Any | None, cfg: Mapping[str, Any] | None, *, ensure: bool = True
) -> PgVectorStore | None:
    """建 store 并（默认）播种空间。

    Args:
        cfg: ``repo.embedding`` 段。
        ensure: 是否在启动时把空间写进 ``vector_spaces`` 并校验。
            **关掉它只在「库还没迁移」的引导场景** —— 正常启动必须开着，
            否则「维度配错了」要等到第一次检索才发现。
    """
    store = build_vector_store(database, cfg)
    if store is not None and ensure:
        await store.ensure_spaces()
    return store
