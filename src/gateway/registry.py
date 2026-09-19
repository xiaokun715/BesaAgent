"""逻辑模型名 → 物理模型 → provider 实例的解析。

**分层解析**（``FR-G-02``）::

    alias（业务写）      "runtime.default"
      └─ candidates       ["gpt-4o-mini", "qwen-plus"]      ← 配置决定
           └─ ModelSpec   provider=openai, model=gpt-4o-mini
                └─ Provider 实例（连接复用，``FR-P-13``）

业务只认第一层。**换模型 = 改配置，业务代码零改动**。

**启动期校验是本模块的核心价值**（``C-3``）。它要区分三类问题，
因为对策完全不同：

========================================  ==========================================
问题                                      处理
========================================  ==========================================
厂商名拼错 / 策略名拼错                     **硬失败** —— 拼错不会自己好
候选引用了不存在的模型键                    **硬失败** —— 引用错误同上
云厂商缺密钥                               软失败：模型标记为**不可用**并保留原因
========================================  ==========================================

第三类必须软失败，否则 ``NFR-G-05``（无密钥可启动）就不成立了。
但**不能直接丢掉**这个模型 —— 丢掉会让「为什么这个候选从没被用到」
变成一个查不到的空缺，而不是一条能读的原因。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from foundation.clock import Clock, SystemClock
from gateway.errors import UnknownAliasError
from gateway.router import STRATEGIES
from gateway.types import AliasSpec, ModelSpec
from provider import build_provider, known_providers
from provider.base import Provider
from provider.types import ModelConfig

__all__ = ["Registry"]

_log = logging.getLogger(__name__)

#: alias 未声明策略时的默认管道
DEFAULT_STRATEGY: tuple[str, ...] = ("capability", "priority")


class Registry:
    """模型注册表。"""

    def __init__(
        self,
        aliases: Mapping[str, AliasSpec],
        models: Mapping[str, ModelSpec],
        *,
        providers_cfg: Mapping[str, Any] | None = None,
        env: Mapping[str, str] | None = None,
        clock: Clock | None = None,
        provider_options: Mapping[str, Any] | None = None,
    ) -> None:
        self._aliases = dict(aliases)
        self._models = dict(models)
        self._providers_cfg = providers_cfg
        self._env = env
        self._clock: Clock = clock or SystemClock()
        #: 原样转交给每个 provider 构造器的额外参数。
        #: **测试注入 ``httpx.MockTransport`` 的唯一入口**（``FR-P-13``）——
        #: 没有它，网关的所有路径都只能靠真实网络测试，而那意味着
        #: 「401 该不该重试」这类断言无法稳定复现。
        self._provider_options = dict(provider_options or {})
        self._providers: dict[str, Provider] = {}

    # ---------------------------------------------------------------- 构建
    @classmethod
    def from_config(
        cls,
        gateway_cfg: Mapping[str, Any],
        *,
        models_cfg: Mapping[str, Any] | None = None,
        providers_cfg: Mapping[str, Any] | None = None,
        env: Mapping[str, str] | None = None,
        clock: Clock | None = None,
        provider_options: Mapping[str, Any] | None = None,
    ) -> Registry:
        """从配置构建并**立即校验**。

        Args:
            gateway_cfg: ``gateway:`` 段（含 ``aliases``；也可内嵌 ``models``）。
            models_cfg: ``models:`` 段。缺省时取 ``gateway_cfg["models"]``。
            providers_cfg: ``providers:`` 段（厂商默认端点与凭据变量名）。
            provider_options: 转交给 provider 构造器（测试注入 ``transport`` / ``clock``）。
        """
        definitions = dict(models_cfg or gateway_cfg.get("models") or {})

        models = {
            key: _build_spec(
                key, raw,
                providers_cfg=providers_cfg, env=env, clock=clock,
                provider_options=provider_options,
            )
            for key, raw in definitions.items()
        }

        aliases = {
            name: _build_alias(name, raw) for name, raw in (gateway_cfg.get("aliases") or {}).items()
        }

        registry = cls(
            aliases, models,
            providers_cfg=providers_cfg, env=env, clock=clock,
            provider_options=provider_options,
        )
        registry.validate()
        return registry

    # ---------------------------------------------------------------- 校验
    def validate(self) -> None:
        """启动期自洽性校验。

        Raises:
            ValueError: 发现**配置错误**（拼错的厂商名、悬空的候选引用、未知策略）。
                这些都必须硬失败 —— 它们不会自己好，且越晚发现代价越大。
        """
        problems: list[str] = []

        for name, alias in self._aliases.items():
            if not alias.candidates:
                problems.append(f"alias {name!r} 没有候选")
            for key in alias.candidates:
                if key not in self._models:
                    known = ", ".join(sorted(self._models)) or "（无）"
                    problems.append(
                        f"alias {name!r} 的候选 {key!r} 未在 models 中定义；已定义：{known}"
                    )
            for strategy in alias.strategy:
                if strategy not in STRATEGIES:
                    known = ", ".join(sorted(STRATEGIES))
                    problems.append(
                        f"alias {name!r} 使用了未知策略 {strategy!r}；已知策略：{known}"
                    )

        unavailable = [spec for spec in self._models.values() if not spec.available]
        if unavailable:
            # **软失败**：只是警告。无密钥可启动是硬需求（NFR-G-05）。
            for spec in unavailable:
                _log.warning("模型 %s 当前不可用：%s", spec.key, spec.unavailable_reason)

        if problems:
            raise ValueError("gateway 配置校验失败：\n  - " + "\n  - ".join(problems))

    # ---------------------------------------------------------------- 查询
    def alias(self, name: str) -> AliasSpec:
        """解析逻辑名。

        Raises:
            UnknownAliasError: 未注册。错误信息**列出全部可用逻辑名** ——
                拼错 alias 是最高频的配置错误，而只说「未注册」会让人去翻文档。
        """
        spec = self._aliases.get(name)
        if spec is None:
            raise UnknownAliasError(name, known=sorted(self._aliases))
        return spec

    def model(self, key: str) -> ModelSpec:
        spec = self._models.get(key)
        if spec is None:
            known = ", ".join(sorted(self._models)) or "（无）"
            raise ValueError(f"未知的模型键 {key!r}；已定义：{known}")
        return spec

    def candidates(self, alias: str) -> list[ModelSpec]:
        """把 alias 展开成 ``ModelSpec`` 列表（顺序保持配置里的顺序）。"""
        return [self.model(key) for key in self.alias(alias).candidates]

    @property
    def aliases(self) -> Mapping[str, AliasSpec]:
        return dict(self._aliases)

    @property
    def models(self) -> Mapping[str, ModelSpec]:
        return dict(self._models)

    # ---------------------------------------------------------------- provider
    def provider(self, key: str) -> Provider:
        """取（并缓存）模型对应的 provider 实例。

        **缓存是 ``FR-P-13`` 的落点**：每个模型一个 provider 实例，
        实例持有复用的 HTTP 连接池。每次调用新建实例 = 每次新建连接池。
        """
        provider = self._providers.get(key)
        if provider is not None:
            return provider

        spec = self.model(key)
        if not spec.available:
            # 直接把注册期记下的原因抛出来，而不是重新构造一次再撞同一个错 ——
            # 后者会丢掉「注册期就知道」这个信息，也会让错误信息每次不一样。
            raise ValueError(f"模型 {key!r} 不可用：{spec.unavailable_reason}")

        built = build_provider(
            dict(spec.config),
            providers=self._providers_cfg,
            env=self._env,
            clock=self._clock,
            **self._provider_options,
        )
        self._providers[key] = built
        return built

    async def aclose(self) -> None:
        """释放全部 provider 的连接。**幂等**。"""
        for provider in self._providers.values():
            await provider.aclose()
        self._providers.clear()


# --------------------------------------------------------------------------- #
# 构建辅助
# --------------------------------------------------------------------------- #


def _build_spec(
    key: str,
    raw: Mapping[str, Any],
    *,
    providers_cfg: Mapping[str, Any] | None,
    env: Mapping[str, str] | None,
    clock: Clock | None,
    provider_options: Mapping[str, Any] | None = None,
) -> ModelSpec:
    # ``provider`` 选**适配器**（协议形状），``vendor`` 选 **providers 段的条目**（端点 + 凭据）。
    # 二者默认相同，所以绝大多数配置只写 ``provider`` 就够了。
    adapter = str(raw.get("provider") or raw.get("type") or "").strip().lower()
    vendor = str(raw.get("vendor") or adapter).strip().lower()

    if not adapter:
        raise ValueError(
            f"model {key!r} 缺少 `provider` 字段；已知厂商：{', '.join(known_providers())}"
        )
    if adapter not in known_providers():
        # **硬失败**：拼错的厂商名不会自己好，且拖到运行时才发现代价更大。
        raise ValueError(
            f"model {key!r} 引用了未知厂商 {adapter!r}；"
            f"已知厂商：{', '.join(known_providers())}"
        )

    # 厂商段缺失**不报错**（适配器类自带默认端点与凭据变量名），但值得提醒：
    # 最危险的形态是「providers 段的键名拼错了」—— 此时配置里的 base_url / api_key_env
    # **静默失效**，转而使用类级默认值。表现为「配了代理却不走代理」，
    # 而没有任何错误信息。用 warning 让这件事至少可见。
    if providers_cfg and vendor not in providers_cfg:
        _log.warning(
            "model %r 的 vendor=%r 未出现在 providers 段中；"
            "将使用厂商内置的默认端点与凭据变量名。"
            "若本意是套用 providers 段的配置，请检查键名拼写。会继续启动。",
            key, vendor,
        )

    declared = ModelConfig.from_config(raw, providers=providers_cfg, env=env)
    priority = int(raw.get("priority") or 0)
    weight = raw.get("weight")
    weight = None if weight is None else float(weight)

    try:
        provider = build_provider(
            raw,
            providers=providers_cfg,
            env=env,
            clock=clock,
            **(provider_options or {}),
        )
        capabilities = provider.capabilities()
        reason = None
    except Exception as exc:  # noqa: BLE001 - 任何构造失败都只让该模型不可用
        # **软失败**：缺密钥、端点非法等都不该阻止进程启动（NFR-G-05）。
        #
        # 能力回退到「配置里显式声明的完整集合」（若配的是列表）——
        # 配的是映射或没配时无法在不知道厂商默认的情况下解析，只能给空集，
        # 该模型会因缺能力而被路由过滤掉，且错误信息会带上 unavailable_reason。
        capabilities = declared.capabilities or frozenset()
        reason = str(exc)

    return ModelSpec(
        key=key,
        # 记 **vendor** 而不是适配器：日志、尝试记录、用量报表里
        # 「deepseek」比「openai」有用得多 —— 后者会让你误以为流量走了真 OpenAI。
        provider=vendor or adapter,
        model=str(raw.get("model") or key),
        capabilities=frozenset(capabilities),
        priority=priority,
        weight=weight,
        config=dict(raw),
        unavailable_reason=reason,
    )


def _build_alias(name: str, raw: Mapping[str, Any]) -> AliasSpec:
    candidates = raw.get("candidates") or ()
    if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
        raise ValueError(
            f"alias {name!r} 的 candidates 必须是列表，得到 {type(candidates).__name__}"
        )
    strategy = raw.get("strategy") or DEFAULT_STRATEGY
    if isinstance(strategy, (str, bytes)):
        strategy = [strategy]
    return AliasSpec(
        alias=name,
        candidates=tuple(str(item) for item in candidates),
        strategy=tuple(str(item) for item in strategy),
    )
