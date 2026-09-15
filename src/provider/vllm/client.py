"""vLLM 的传输客户端。

与基类的**唯一**差异：``requires_auth = False``。

这不是可有可无的一行 —— 自建 vLLM 通常不开鉴权，若沿用基类行为，
在 ``api_key`` 为空时会发出 ``Authorization: Bearer ``（空值）。
这个头会被部分反向代理与网关判为**「鉴权失败」而不是「无需鉴权」**，
表现为一个莫名其妙的 401，而配置看起来完全正确（FR-P-12 / 验收场景 A-3）。
"""

from __future__ import annotations

from provider.openai.client import HttpClient

__all__ = ["VLLMClient"]


class VLLMClient(HttpClient):
    """自建 vLLM 的传输客户端：**不发 ``Authorization`` 头**。"""

    provider_name = "vllm"
    requires_auth = False
