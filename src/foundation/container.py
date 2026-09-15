"""运行时装配中心：配置 + 连接 + model 实例的持有与释放。

**职责**：

1. 持有 ``settings``（唯一配置事实来源）；
2. **懒建**连接（PostgreSQL / Redis）并在关停时统一释放；
3. 把 model 配置**投影厂商默认值**（委托 ``foundation.provider``）；
4. 按 ``(group, name)`` **缓存** model 实例。

**第 4 条是硬需求，不是优化**：需求说明书-provider FR-P-13 要求 HTTP 客户端复用。
若每个调用都新建 provider 实例，就等于每次都新建连接池 ——
在多 agent 并发场景下会直接打爆 fd 上限，且 TLS 握手开销会体现在每个请求的延迟里。

**生命周期必须成对**：所有连接与客户端都实现 ``aclose``，由本模块在关停时释放。
构造了不释放 = 连接泄漏，在长跑的 agent 进程里表现为「跑几小时就连不上上游」，
而且**症状离原因很远**（泄漏点在上次启动，报错在当前）。

**明确不做的事**：

- **不 import 任何业务模块**。本模块属于 foundation，依赖方向只出不进。
- **不做组合**。把 gateway / provider / repo / event 真正接到一起的代码放在
  **组合根** ``apps/*/runtime/``，那里是唯一允许 import 全部同仓库模块的地方。
  放在 foundation 会让底座反向依赖业务，是分层崩塌最常见的起点。

**与 besa-iv-kb 的差异**：那边叫 ``Runtime``，用全局 ``Registry`` 按 ``(kind, type)``
解析；本仓库按能力各自持有 factory，Container 只做「持有 + 缓存 + 释放」三件事，
查找逻辑下沉给 ``foundation.factory`` 与各模块的注册表。
"""
