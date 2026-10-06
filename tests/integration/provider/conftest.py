"""真实厂商测试的夹具 —— **默认不跑**，要显式打开。

## 为什么单独一组、且默认跳过

``tests/unit/`` 那一整套全部注入 ``httpx.MockTransport``，零真实网络请求
（这是项目自己的约定：网关的多数断言依赖**精确的调用次数**，真实网络下那些数字不可复现）。

但 mock 替换掉的正是**网络边界**，而边界上有一整类问题只在真实厂商那里才暴露：

============================  ============================================
mock 测得出                    mock 测不出
============================  ============================================
编排逻辑（选谁/重试/降级/预算）  协议兼容（路径、字段名、SSE 形状）
参数透传                      厂商怪癖（推理字段、index 分片重置）
错误归一化的**分支**          鉴权失败是不是真的映射成哪个异常
============================  ============================================

这不是假设。本仓库踩过的两个真 bug，mock 测试**全绿**：

1. ``_endpoint()`` 被改名脚本改成 ``/runtime/completions`` —— MockTransport
   **按 host 分流、压根不看路径**，所以全部用例照过；只有真实调用才 404；
2. embedding 的 ``index`` 在批量 ≥9 条时被上游按 8 条分片重置 ——
   上游才有的行为，mock 造不出来。

所以这一组的定位是**只覆盖 mock 测不出的那几件**，不重复单测已经覆盖的逻辑。

## 怎么跑

::

    BESA_LIVE_TESTS=1 PYTHONPATH=src python -m pytest tests/integration/provider -v

密钥从仓库根 ``.env`` 或进程环境读（与 ``foundation.settings`` 的取法一致）。
缺哪个厂商的密钥，只跳过那个厂商的用例。

**CI 里不该跑这一组**：它花钱、慢、且会随上游版本变化而红。
要挂就挂成手动触发（``workflow_dispatch``）。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from provider import build_provider

__all__ = ["DEEPSEEK_BASE_URL", "SILICONFLOW_BASE_URL"]

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"

#: 实测可用的名字。**不要写 ``deepseek-v4-flash[1m]``** —— 那个 ``[1m]`` 是
#: Claude Code 客户端的约定（1M 上下文），不是 API 的模型名，发过去会得到 400：
#: ``The supported API model names are deepseek-flash, deepseek-v4-pro``。
DEEPSEEK_MODEL = "deepseek-v4-flash"
SILICONFLOW_EMBEDDING_MODEL = "Qwen/Qwen3-VL-Embedding-8B"

#: 真实调用比 mock 慢得多；给足超时，避免把「网络慢」误报成「协议不对」
TIMEOUT_S = 120.0

_REPO_ROOT = Path(__file__).resolve().parents[3]


def load_dotenv() -> dict[str, str]:
    """读仓库根 ``.env``，进程环境优先。

    与 ``foundation.settings`` 的取法一致 —— 那边也是「进程环境优先，``.env`` 兜底」。
    这里手读一遍是因为这一组测试要的是**原始密钥**，而不是走一遍配置投影。
    """
    env = dict(os.environ)
    dotenv = _REPO_ROOT / ".env"
    if dotenv.exists():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env.setdefault(key.strip(), value.strip())
    return env


@pytest.fixture(scope="session")
def live_env() -> dict[str, str]:
    """打开这一组的总开关。"""
    if os.environ.get("BESA_LIVE_TESTS") != "1":
        pytest.skip(
            "真实厂商测试默认不跑（它花钱、慢、且会随上游版本变化）。"
            "显式打开：BESA_LIVE_TESTS=1"
        )
    return load_dotenv()


def _require(env: Mapping[str, str], var: str) -> str:
    key = str(env.get(var) or "").strip()
    if not key:
        pytest.skip(f"没有 {var}（放仓库根 .env 或进程环境里）")
    return key


@pytest.fixture
def deepseek(live_env: dict[str, str]):
    """装配一个**真的会发请求**的 DeepSeek provider。

    刻意**不注入 transport** —— 这一组的意义就是走真实网络。
    """
    _require(live_env, "DEEPSEEK_API_KEY")

    def _make(**over: Any):
        # ``transport`` / ``clock`` **不是模型配置项**，要传给 ``build_provider`` ——
        # 混进 cfg 里会被静默忽略（配置里的未知键不报错），于是 provider 照旧走真实
        # 网络，而调用方以为已经注入了 spy。这类「参数没透传」正是这一组要防的东西。
        transport = over.pop("transport", None)
        clock = over.pop("clock", None)

        cfg: dict[str, Any] = {
            "provider": "openai",          # 适配器：DeepSeek 是 OpenAI 兼容形状
            "vendor": "deepseek",          # 端点与凭据取自 providers.deepseek
            "model": DEEPSEEK_MODEL,
            "temperature": 0.0,
            "max_tokens": 1024,
            "timeout_s": TIMEOUT_S,
            "retries": 1,
            "capabilities": {"tools": True, "json": True},
        }
        cfg.update(over)
        return build_provider(
            cfg,
            providers={
                "deepseek": {
                    "base_url": DEEPSEEK_BASE_URL,
                    "api_key_env": "DEEPSEEK_API_KEY",
                }
            },
            env=live_env,
            transport=transport,
            clock=clock,
        )

    return _make


@pytest.fixture
def siliconflow(live_env: dict[str, str]):
    """装配一个真的会发请求的 SiliconFlow embedding provider。"""
    _require(live_env, "SILICONFLOW_API_KEY")

    def _make(**over: Any):
        transport = over.pop("transport", None)
        clock = over.pop("clock", None)

        cfg: dict[str, Any] = {
            "provider": "openai",
            "vendor": "siliconflow",
            "model": SILICONFLOW_EMBEDDING_MODEL,
            "timeout_s": TIMEOUT_S,
            # **完整声明**能力（列表 = 替换厂商默认），否则会继承 chat 的能力集
            "capabilities": ["embedding"],
        }
        cfg.update(over)
        return build_provider(
            cfg,
            providers={
                "siliconflow": {
                    "base_url": SILICONFLOW_BASE_URL,
                    "api_key_env": "SILICONFLOW_API_KEY",
                }
            },
            env=live_env,
            transport=transport,
            clock=clock,
        )

    return _make
