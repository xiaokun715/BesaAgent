"""厂商适配层：把各家 API 的差异收敛在边界内。

**分层规则**（可由 import-linter 机械校验，见《架构概要设计-provider》§1.2）：

    provider/types.py  provider/errors.py     契约层，不依赖任何厂商子包
    provider/base.py                           契约层，不依赖任何厂商子包
    provider/<厂商>/*                          只依赖契约层与 foundation
    src/gateway                                唯一消费者

**本模块（``__init__``）是唯一的例外**：它需要认识所有厂商才能按名字装配，
所以它 import 了全部子包。为了不让「只想拿契约」的人也拖上 ``httpx``，
厂商类的 import 是**延迟**的（在 :func:`get_provider_class` 内部）——
``import provider`` 本身只加载契约层。
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from typing import Any

from provider.base import ChatModel, Client, EmbeddingModel, Provider
from provider.errors import ProviderError
from provider.types import ChatRequest, ChatResponse, Message, ModelConfig

__all__ = [
    "PROVIDER_CLASSES",
    "ChatModel",
    "ChatRequest",
    "ChatResponse",
    "Client",
    "EmbeddingModel",
    "Message",
    "ModelConfig",
    "Provider",
    "ProviderError",
    "build_provider",
    "get_provider_class",
    "known_providers",
    "register_provider",
]

#: 厂商名 → ``模块路径:类名``。
#:
#: **值写成字符串而不是类，是为了延迟 import**：否则 ``import provider`` 会连带
#: 加载四家厂商及其 ``httpx`` 依赖，而契约层的使用方（类型检查、单测契约）并不需要它。
PROVIDER_CLASSES: dict[str, str] = {
    "mock": "provider.mock.provider:MockProvider",
    "openai": "provider.openai.provider:OpenAIProvider",
    "dashscope": "provider.dashscope.provider:DashScopeProvider",
    "vllm": "provider.vllm.provider:VLLMProvider",
}

#: 运行时注册的额外厂商（插件式扩展）。优先于内置表。
_EXTRA: dict[str, str] = {}


def register_provider(name: str, target: str) -> None:
    """注册一个额外厂商。

    Args:
        name: 配置里 ``provider:`` 用的名字。
        target: ``"模块路径:类名"``。需要 import 时提供。
    """
    key = name.strip().lower()
    if not key:
        raise ValueError("厂商名不能为空")
    _EXTRA[key] = target


def known_providers() -> tuple[str, ...]:
    """全部已知厂商名（内置 + 运行时注册）。

    **供启动期校验用**：``src/gateway`` 需要在注册表构建时区分
    「厂商名拼错了」与「厂商认识但这台机器上没配密钥」——
    前者必须**硬失败**（拼错不会自己好），后者只需把模型标记为不可用。
    没有这个函数，两者都会退化成同一个模糊的构造异常。
    """
    return tuple(sorted({*PROVIDER_CLASSES, *_EXTRA}))


def get_provider_class(name: str) -> type[Provider]:
    """按名字取厂商类。

    Raises:
        ValueError: 名字未注册。错误信息会列出**全部已知厂商** ——
            「provider 拼错」是最高频的配置错误，而只报「未注册」会让人去翻文档。
    """
    key = (name or "").strip().lower()
    target = _EXTRA.get(key) or PROVIDER_CLASSES.get(key)
    if not target:
        known = sorted({*PROVIDER_CLASSES, *_EXTRA})
        raise ValueError(f"未知的 provider={name!r}；已注册的厂商：{', '.join(known)}")

    module_path, _, attribute = target.partition(":")
    module = importlib.import_module(module_path)
    return getattr(module, attribute)


def build_provider(
    model_cfg: Mapping[str, Any],
    *,
    providers: Mapping[str, Any] | None = None,
    transport: Any | None = None,
    env: Mapping[str, str] | None = None,
    clock: Any | None = None,
    **options: Any,
) -> Provider:
    """从一段 model 配置装配出 provider 实例 —— ``container`` 的主入口。

    厂商名取自 ``provider``，缺失时回退到 ``type``（沿用 ``besa-iv-kb`` 的配置习惯，
    那边所有能力都用 ``type`` 选实现）。

    ``**options`` 原样转交厂商构造器（如 ``MockProvider`` 的 ``script``）。
    """
    raw = model_cfg.get("provider") or model_cfg.get("type")
    if not raw:
        raise ValueError(
            "model 配置缺少 `provider`（或旧写法 `type`）字段；"
            f"已知厂商：{', '.join(sorted({*PROVIDER_CLASSES, *_EXTRA}))}"
        )

    klass = get_provider_class(str(raw))
    return klass.from_config(
        model_cfg,
        providers=providers,
        transport=transport,
        env=env,
        clock=clock,
        **options,
    )
