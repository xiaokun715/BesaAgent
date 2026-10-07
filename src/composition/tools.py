"""把工具层装配起来，并把它接到 gateway 上。

**这个文件存在是因为两条依赖方向在这里交叉**：

- ``src/tool`` **不能** import ``src/provider``（契约 4 机械强制）——
  所以「工具定义 → 厂商要的 ``ToolSpec``」这一步必须发生在**同时看得见两边**的地方；
- ``src/tool`` **不能** import ``apps/server/storage/redis`` ——
  所以幂等存储由 app 层建好后**注入**（与 ``Database`` 完全同构）。

组合根是唯一同时满足这两条的地方。这不是权宜 ——
它是「契约不给任何模块开豁免」这条纪律的直接推论。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from foundation.database import Database
from gateway.gateway import Gateway
from gateway.tools import ToolSpec, tool_spec
from tool.catalog import Catalog, Embedder
from tool.executor import CallStack, EventSink, ToolExecutor
from tool.idempotency import DEFAULT_ON_UNAVAILABLE, IdempotencyGuard, UnavailableStore
from tool.injection import InjectionPolicy, Inspector
from tool.permission import PermissionPolicy
from tool.recovery import DEFAULT_REFUSE_AFTER, RetryBreaker
from tool.registry import build_default_registry
from tool.result import ResultPolicy, ResultProcessor
from tool.sandbox import Sandbox, SandboxLimits
from tool.types import ToolDefinition

__all__ = ["GatewayEmbedder", "ToolsBundle", "build_tools", "to_tool_specs"]

_log = logging.getLogger(__name__)


class EmbedderStore(Protocol):
    """幂等存储的形状（``IdempotencyStore``）。

    这里只用 Protocol 声明，**不 import 实现** —— 实现在 app 层。
    """

    @property
    def available(self) -> bool: ...


class GatewayEmbedder:
    """用 gateway 的 embedding 逻辑名做向量化。

    **复用而不是另引一个**：否则「相似」这个词会有两个口径
    （工具体检一个、检索一个），而它们会漂移。

    **失败不阻断启动**：``Catalog.inspect`` 会捕获异常并告警 ——
    与「缺密钥的模型软失败」同一条纪律。
    """

    def __init__(self, gateway: Gateway, alias: str = "emb.default") -> None:
        self._gateway = gateway
        self._alias = alias

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        result = await self._gateway.embed(self._alias, list(texts))
        return tuple(result.response.vectors)


@dataclass(frozen=True)
class ToolsBundle:
    """装配好的工具层。"""

    executor: ToolExecutor
    catalog: Catalog
    sandbox: Sandbox


def build_tools(
    *,
    tool_cfg: Mapping[str, Any] | None,
    idempotency_cfg: Mapping[str, Any] | None,
    gateway: Gateway,
    database: Database | None,
    store: Any | None,
    events: EventSink | None,
) -> ToolsBundle:
    """按配置装配工具层。

    Args:
        tool_cfg: ``tool:`` 段。
        idempotency_cfg: ``idempotency:`` 段。
        gateway: 用来做 embedding（工具体检与检索）—— 它必须已经建好。
        database: 权威记录。``None``（CLI 默认之外的情况）会让有副作用的工具被拒。
        store: 幂等快路径（Redis）。``None`` → 退到「只用权威记录」的慢路径。
        events: 事件出口。**与 gateway 共用同一个实例** ——
            于是用量与工具事件落进同一个事务（`T-M`）。
    """
    tool_data = dict(tool_cfg or {})
    idem_data = dict(idempotency_cfg or {})

    sandbox = Sandbox(SandboxLimits.from_config(tool_data))
    registry = build_default_registry(
        enabled=tool_data.get("enabled"), sandbox=sandbox
    )

    # 幂等配置里的 on_unavailable 直接透传给 guard —— 那里有默认值，
    # 但**配置里必须显式写**（`C-T-3`），这条由 configs/base.yaml 保证。
    on_unavailable = idem_data.get("on_unavailable") or DEFAULT_ON_UNAVAILABLE

    guard = IdempotencyGuard(
        store if store is not None else UnavailableStore(),
        database,
        lease_s=float(idem_data.get("lease_s", 120)),
        result_ttl_s=float(idem_data.get("result_ttl_s", 3600)),
        max_cached_result_bytes=int(idem_data.get("max_cached_result_bytes", 65_536)),
        on_unavailable=on_unavailable,  # type: ignore[arg-type]
    )

    limits = dict(tool_data.get("limits") or {})
    executor = ToolExecutor(
        registry=registry,
        permission=PermissionPolicy.from_config(tool_data),
        guard=guard,
        inspector=Inspector(InjectionPolicy.from_config(tool_data)),
        results=ResultProcessor(ResultPolicy.from_config(tool_data)),
        breaker=RetryBreaker(
            refuse_after=int(limits.get("refuse_after_failures", DEFAULT_REFUSE_AFTER))
        ),
        calls=CallStack(
            max_depth=int(limits.get("max_call_depth", 8)),
            max_calls_per_run=int(limits.get("max_calls_per_run", 200)),
        ),
        events=events,
        key_field=str(idem_data.get("key_field", "idempotency_key")),
        limits=limits,
    )

    catalog_cfg = dict(tool_data.get("catalog") or {})
    catalog = Catalog(
        tools=registry.tools,
        embedder=GatewayEmbedder(gateway, str(catalog_cfg.get("embedding_alias", "emb.default"))),
        duplicate_threshold=float(catalog_cfg.get("duplicate_similarity_threshold", 0.92)),
        selection=str(catalog_cfg.get("selection", "category")),  # type: ignore[arg-type]
        rag_top_k=int(catalog_cfg.get("rag_top_k", 20)),
    )

    _log.info(
        "工具层装配完成：%d 个工具（%s），沙箱 %s，幂等=%s",
        len(registry),
        ", ".join(registry.names),
        sandbox.mode,
        "开" if store is not None or database is not None else "关",
    )
    return ToolsBundle(executor=executor, catalog=catalog, sandbox=sandbox)


def to_tool_specs(definitions: Sequence[ToolDefinition]) -> tuple[ToolSpec, ...]:
    """把工具定义转成厂商要的 ``ToolSpec``（`DT-5` 的转换点）。

    **这一步只能在这里做**：``src/tool`` 不能 import ``src/provider``（契约 4），
    而 ``src/gateway`` 也不该知道工具 —— 它只管「把给的参数传给模型」。

    转换本身很薄（三个字段同名），**薄是对的** ——
    它证明了两边的契约形状本来就一致；一旦哪天要在这里做映射表，
    说明工具层的定义开始泄漏厂商细节了，那是该停下来看的信号。
    """
    return tuple(
        tool_spec(
            name=definition.name,
            description=definition.description,
            parameters=definition.parameters,
        )
        for definition in definitions
    )
