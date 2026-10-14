"""把记忆层装配起来，并把它接到 gateway 与 repo 上。

**这个文件存在，是因为三条依赖方向在这里交叉**（与 ``composition/tools.py`` 同构）：

- ``src/memory`` **不能** import ``provider``（契约 4）—— 所以「一次补全」这一步
  由这里适配：把 ``ChatModel`` 协议接到 ``Gateway.chat`` 上；
- ``src/memory`` **不能** import ``apps/server/storage`` —— 所以 ``VectorStore``
  由 app 层建好后**注入**（与 ``Database`` 完全同构）；
- ``src/memory`` 需要**每个事务一个** ``MemoryStore`` —— 组合根交出工厂函数，
  记忆层不必知道仓储是怎么拼起来的。

**没有数据库 / 没开记忆时**：``build_memory`` 返回 ``None``，组合根把 ``Runtime.memory``
置空。记忆是**旁路**，不是硬依赖（`NFR-M-07`）。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from composition.tools import GatewayEmbedder

from gateway.gateway import Gateway
from gateway.messages import system as system_message
from gateway.messages import user as user_message
from memory.decay import DecayPolicy
from memory.extractor import ExtractPolicy, Extractor
from memory.manager import MemoryLimits, MemoryService
from memory.policy import GatePolicy, WriteGate
from memory.reflect import ReflectPolicy, Reflector
from memory.resolver import Resolver
from memory.retriever import RetrievalPolicy, Retriever
from memory.scheduler import MaintenancePolicy
from memory.store import MemoryStore
from repo.memory import DEFAULT_VECTOR_SPACE, MemoryRepo

__all__ = ["GatewayChat", "MemoryBundle", "build_memory"]

_log = logging.getLogger(__name__)


class GatewayChat:
    """用 gateway 的逻辑别名做一次补全。

    **是适配器不是实现**：``src/memory`` 只认 ``ChatModel`` 协议，
    这里把协议翻译成 ``Gateway.chat`` —— 于是记忆层既不认识 ``Gateway``，
    也不认识任何厂商协议，而重试/降级/限流/计量**全部**仍然走网关那一条路。
    """

    def __init__(self, gateway: Gateway, alias: str = "runtime.default") -> None:
        self._gateway = gateway
        self._alias = alias

    async def complete(
        self,
        *,
        system: str,
        prompt: str,
        json_schema: Mapping[str, Any] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        result = await self._gateway.chat(
            self._alias,
            [system_message(system), user_message(prompt)],
            json_schema=json_schema,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return result.content


@dataclass(frozen=True)
class MemoryBundle:
    """装配好的记忆层。"""

    service: MemoryService
    limits: MemoryLimits
    decay: DecayPolicy
    scheduler: MaintenancePolicy


def build_memory(
    *,
    memory_cfg: Mapping[str, Any] | None,
    gateway: Gateway,
    database: Any | None,
    vector_store: Any | None,
    events: Any | None,
    clock: Any | None = None,
    space: str = DEFAULT_VECTOR_SPACE,
) -> MemoryBundle | None:
    """按配置装配记忆层。**未启用或没有数据库时返回 ``None``。**

    Args:
        space: 向量空间名。**来自 ``repo.embedding.space``**（与 repo 同一份配置）——
            记忆的向量和别的向量共用同一套空间定义，不该各配一个。
    """
    cfg = dict(memory_cfg or {})
    limits = MemoryLimits.from_config(cfg)
    if not limits.enabled:
        _log.info("记忆层未启用（memory.enabled=false）—— 这是默认值，开启它意味着开始长期保存用户信息")
        return None
    if database is None:
        # 没有数据库时记忆退化成「只能读、不能写」，而一条不能写的记忆没有意义 ——
        # 直接不装配，比装配一个半残的服务好。
        _log.warning("memory.enabled=true 但没有 Database —— 记忆层不装配（记忆是旁路，不是硬依赖）")
        return None

    gates = _mapping(cfg.get("gates"))
    write = _mapping(cfg.get("write"))
    extract_cfg = _mapping(write.get("extract"))
    judge_cfg = _mapping(gates.get("llm_judge"))

    gate_policy = GatePolicy.from_config(cfg)
    retrieval = RetrievalPolicy.from_config(cfg.get("retrieve"), _mapping(write.get("retrieve")))
    decay = DecayPolicy.from_config(cfg.get("decay"))
    scheduler = MaintenancePolicy.from_config(cfg.get("maintenance"))

    chat = GatewayChat(gateway, _chat_alias(judge_cfg, extract_cfg))
    embedder = GatewayEmbedder(gateway, _embedding_alias(cfg))

    def store_factory(tx: Any) -> MemoryStore:
        return MemoryStore(MemoryRepo(tx, vector_store, space=space))

    service = MemoryService(
        database=database,
        store_factory=store_factory,
        gate=WriteGate(chat, gate_policy),
        extractor=Extractor(chat, ExtractPolicy.from_config(extract_cfg)),
        retriever=Retriever(embedder, retrieval),
        resolver=Resolver(
            chat,
            similarity_threshold=retrieval.similarity_threshold,
            merge_threshold=retrieval.merge_threshold,
        ),
        reflector=Reflector(chat, ReflectPolicy.from_config(cfg.get("reflect"))),
        decay=decay,
        scheduler=scheduler,
        limits=limits,
        embedder=embedder,
        clock=clock,
        events=events,
    )

    _log.info(
        "记忆层装配完成：作用域上限=%s，半衰期=%s 天，向量=%s，维护=%s",
        ", ".join(gate_policy.allow_scopes) or "无",
        decay.half_life_days,
        "开" if vector_store is not None else "关（退到结构化 + 关键词）",
        "开" if scheduler.enabled else "关",
    )
    return MemoryBundle(service=service, limits=limits, decay=decay, scheduler=scheduler)


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _chat_alias(judge_cfg: Mapping[str, Any], extract_cfg: Mapping[str, Any]) -> str:
    """判断、提取、反思共用一个逻辑别名；没配就退到 ``runtime.default``。"""
    for source in (judge_cfg, extract_cfg):
        alias = source.get("alias")
        if alias:
            return str(alias)
    return "runtime.default"


def _embedding_alias(cfg: Mapping[str, Any]) -> str:
    write = _mapping(cfg.get("write"))
    retrieve = _mapping(write.get("retrieve"))
    return str(retrieve.get("embedding_alias") or "emb.default")
