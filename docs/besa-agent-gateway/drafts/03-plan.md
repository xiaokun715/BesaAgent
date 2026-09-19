# 03 分析规划

## 规模与模式

| 项 | 值 |
|---|---|
| 目标 | `E:\aloha\AIComponents\besa-agent\src\gateway` |
| 文件数 | 11（1 个空 `__init__.py`） |
| 总行数 | 2860 |
| 有效代码行 | 2198 |
| 最大文件 | `gateway.py` 865 行（33%） |
| 配套文档 | `docs/架构概要设计-gateway.md` 480 行、`docs/需求说明书-gateway.md` 424 行 |
| 单测 | 900 行 |

**用户选择：深度分析** —— 核心模块 ≥90%、次要模块 ≥60%。
本模块规模小（<3000 行），深度分析成本可控，目标是**逐行读完 11 个文件**。

## 用户决策

| 问题 | 选择 |
|---|---|
| 分析模式 | 深度分析（核心 ≥90%、次要 ≥60%） |
| 报告开头 | **精简** —— 跳过竞品定位，一段话交代问题后直接进项目全景 |
| 深入重点 | **全部四个子系统**（编排+重试预算 / 路由+注册表 / 限流+熔断+降级 / 成本+用量台账） |
| 实测校验 | **加入** —— 用真实 API（DeepSeek / SiliconFlow）跑出来的行为差异写进报告 |

## 模块划分（按内聚职责，不按目录顺序）

| # | 模块 | 文件 | 行数 | 类型 |
|---|---|---|---:|---|
| M1 | 编排骨架 | `gateway.py` | 865 | 核心 |
| M2 | 选择与装配 | `router.py` `registry.py` `types.py` | 744 | 核心 |
| M3 | 失败路径与错误语义 | `retry.py` `fallback.py` `errors.py` | 430 | 核心 |
| M4 | 可用性防护 | `rate_limit.py` `health.py` | 509 | 核心 |
| M5 | 计量 | `cost.py` `usage.py` | 312 | 核心 |

合计 2860 行。**五个模块全部是核心模块**（用户要求全子系统深入）。

## 覆盖率目标

| 模块 | 文件总行 | 目标覆盖率 | 目标已读行 |
|---|---:|---:|---:|
| M1 | 865 | ≥90% | ≥779 |
| M2 | 744 | ≥90% | ≥670 |
| M3 | 430 | ≥90% | ≥387 |
| M4 | 509 | ≥90% | ≥458 |
| M5 | 312 | ≥90% | ≥281 |

## 实测校验素材（已在本会话获得，无需重跑）

以下数据来自真实调用 DeepSeek（`api.deepseek.com/v1`）与 SiliconFlow（`api.siliconflow.cn/v1`），
均为 gateway 层行为，写入报告第 7 章：

1. **降级链的 attempts 记录**（符合 FR-G-11）：候选链 [坏 key, 好模型] → 两条 AttemptRecord 都保留，
   第一条 `outcome=failed retryable=False error="鉴权失败：密钥无效或无权限 HTTP 401"`，
   第二条 `outcome=success`，`degraded=True`。
2. **流式调用成本恒为「未知」**：同一次调用，非流式 `cost=0.000089 CNY (known=True)`，
   流式 `cost=未知 (amount=None)`。根因在 `gateway.py` 的 `_stream_impl`：
   `stream_chat` 的契约是 `AsyncIterator[str]`，没有承载 usage 的位置，构造的 ChatResponse 用量全 `None`。
   代码注释已把它标为**已知限制、一期不做**（需要 `stream_options.include_usage` + 契约变更）。
3. **`deadline_s` 只拦「新的上游调用」，不取消在途请求**：`deadline_s=0.001` 时单候选调用仍跑满 0.56s；
   两个候选时第 2 跳确实被拦下并抛 `BudgetExhaustedError（deadline）`，错误原因的区分（deadline vs attempts）正确。
4. **成本 `None` 语义的两处细节**：`cost.py` 用 `and` 判断（输入输出**都**未知才算未知），
   `or 0` 兜住单侧未知；实测 embedding 的 `output_tokens=None` 未导致成本变成未知。
5. **未知 alias 的错误信息**：`UnknownAliasError` 列出全部可用逻辑名（符合 FR-G-02/B-3）。
6. **provider 侧一处会穿透到 gateway 的实测缺陷**（已修复）：SiliconFlow 批量 ≥9 条时上游 `index`
   按 8 条分片重置，而 provider 原先无条件按 index 重排 → 静默错位。它说明
   「上游返回的东西要验一遍再说」这条原则在 gateway 的边界上同样适用。

## 报告结构（阶段 5 设计）

见 `05-modules-plan.md`。
