"""厂商（provider）抽象：一个厂商供应多种能力，共享 base_url 与凭据。

**问题**：同一个厂商的身份散在多处 —— provider 名、环境变量前缀、
adapter 里的类级默认 URL。``src/provider`` 下有 openai / dashscope / vllm 三家，
若每家的 base_url 与凭据各写各的，换地址要改 N 个地方，且没有单一事实来源。

**做法**（纯配置层，不碰适配器）::

    providers:
      openai:
        base_url: https://api.openai.com/v1
        api_key_env: OPENAI_API_KEY
      vllm-local:
        base_url: http://127.0.0.1:8000/v1
        # 本地端点：不配密钥，且**必须允许不配**

    models:
      chat-default: { provider: openai, model: gpt-4o-mini, temperature: 0.0 }

**投影规则**：model 级显式值**覆盖**厂商段默认值；不写 ``provider`` 的 model 走旧行为。
新增一个厂商 = ``providers:`` 加一段 + ``models:`` 加一个引用它的条目，**仅此而已**。

**凭据解析优先级**（需求说明书-provider FR-P-12）::

    模型级 api_key > 模型级 api_key_env > 厂商段 api_key_env > 厂商约定环境变量

前三级由本模块的 :func:`resolve_api_key` 一次算完（厂商段的值在 :func:`project_model_config`
里已经合并到 model 级）；第四级（``Provider.API_KEY_ENV`` 类属性）由 ``src/provider`` 兜底，
因为那是适配层的知识，不是配置层的。

**本模块只做投影与解析，不构造实例**：真正读这些值的是 ``src/provider/<厂商>/provider.py``。
把「配置的厂商抽象」放在 foundation，是为了让适配层不必知道配置文件的形状 ——
**换配置格式时不改适配器**。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["ProviderDefaults", "project_model_config", "resolve_api_key"]

#: 会被「厂商段 → model 级」投影的字段。刻意只列这三个：
#: 其余字段（temperature / max_tokens …）属于**模型**语义，由厂商段提供会掩盖真实来源。
_PROJECTED_FIELDS: tuple[str, ...] = ("base_url", "api_key_env", "api_version")


@dataclass(frozen=True)
class ProviderDefaults:
    """厂商段的默认值。字段缺失即空串 —— 与「显式配置为空」不做区分，因为没有意义。"""

    name: str = ""
    base_url: str = ""
    api_key_env: str = ""
    api_version: str = ""


def project_model_config(
    model_cfg: Mapping[str, Any],
    providers: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """把 model 配置与它引用的厂商段默认值合并成一个扁平映射。

    ``None`` 的字段**不覆盖**厂商默认值 —— 这与「显式写空串」语义不同：
    配置里写 ``base_url: null`` 或干脆不写，都表示「用厂商段的」；
    写 ``base_url: ""`` 则表示「就是空的」，此时应当报错而非静默回退。

    Args:
        model_cfg: ``models:`` 段下的单个条目。
        providers: ``providers:`` 段整体；为 ``None`` 时跳过投影（模型自带全部字段）。

    Returns:
        合并后的映射（**新对象**，不修改入参）。
    """
    merged: dict[str, Any] = {k: v for k, v in model_cfg.items() if v is not None}

    # ``provider`` 选**适配器**（用哪种协议形状），``vendor`` 选**providers 段的条目**
    # （端点与凭据）。二者默认相同，所以绝大多数配置只需要写 ``provider``。
    #
    # 分开的必要性来自一个真实场景：DeepSeek / SiliconFlow / Moonshot 这些厂商
    # 走的是 **OpenAI 兼容协议**，适配器都该是 ``openai``，但端点与密钥各不相同。
    # 若只有 ``provider`` 一个字段，就只能二选一 ——
    # 要么共享真 OpenAI 的 base_url（错），要么在每个 model 上重复写 base_url + api_key_env（啰嗦且易漂移）。
    name = str(merged.get("vendor") or merged.get("provider") or "").strip()
    if not name or not providers:
        return merged

    raw_defaults = providers.get(name)
    if raw_defaults is None:
        # 厂商段缺失**不是错误**：适配器类自带 DEFAULT_BASE_URL 与 API_KEY_ENV
        # （``provider.base.Provider`` 的三个类属性），所以 ``provider: mock``
        # 这种无需任何配置的厂商必须能直接工作。
        #
        # 「未知厂商」的检测**刻意不在这里** —— 本模块属于 foundation，
        # 看不到 ``provider.PROVIDER_CLASSES`` 那张清单。放在这里只能靠
        # 「providers 段里有没有」来猜，而那是猜错的：mock 有类级默认端点却不在配置里。
        # 真正知道厂商清单的是 ``gateway.registry``，由它做硬校验。
        return merged
    if not isinstance(raw_defaults, Mapping):
        raise ValueError(f"providers.{name} 必须是映射，得到 {type(raw_defaults).__name__}")

    for field_name in _PROJECTED_FIELDS:
        if merged.get(field_name):
            continue                      # model 级显式值优先
        value = raw_defaults.get(field_name)
        if value:
            merged[field_name] = value
    return merged


def resolve_api_key(
    cfg: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """按优先级解析密钥，返回空串表示「没有」。

    **返回空串而不是抛错**：是否需要密钥是**适配层**的知识
    （``Provider.REQUIRES_API_KEY``）—— 云端厂商缺失要 fail fast，
    本地端点缺失是正常状态。配置层不该替适配层做这个判断。

    Args:
        cfg: 已经过 :func:`project_model_config` 的映射（或原始 model 配置）。
        env: 环境变量来源，**仅供测试注入**；``None`` 时读 ``os.environ``。

    Returns:
        密钥本体；没有则空串。**调用方不得把返回值写进日志或异常消息**
        （需求说明书-provider NFR-P-04）—— 需要排障时只输出变量名。
    """
    source = os.environ if env is None else env

    # 1) 模型级直填。允许，但配置里出现它本身就是可疑的：密钥进了版本库或日志。
    direct = str(cfg.get("api_key") or "").strip()
    if direct:
        return direct

    # 2/3) 模型级 api_key_env（厂商段的值在投影阶段已合并进来）
    var_name = str(cfg.get("api_key_env") or "").strip()
    if var_name:
        value = str(source.get(var_name) or "").strip()
        if value:
            return value

    # 4) 厂商约定环境变量由适配层（Provider.API_KEY_ENV）兜底，不在配置层猜。
    return ""
