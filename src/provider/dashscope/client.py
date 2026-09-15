"""DashScope（阿里百炼）的传输客户端。

走 **OpenAI 兼容模式**（``/compatible-mode/v1``），因此与基类差异极小 ——
这正是选择兼容模式而非原生协议的收益（架构概要设计-provider §9 D-B 已定）。
原生协议里那些没有对应物的参数（如 ``enable_search``）通过 model 配置的
``extra`` 透传，不需要在这里开口子。
"""

from __future__ import annotations

from provider.openai.client import HttpClient

__all__ = ["DashScopeClient"]


class DashScopeClient(HttpClient):
    """DashScope 兼容模式的传输客户端。当前无需覆写任何行为。"""

    provider_name = "dashscope"
