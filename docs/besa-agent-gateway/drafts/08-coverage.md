# 08 覆盖率汇总

分析模式：**深度分析**（核心 ≥90%、次要 ≥60%）。本模块 5 个模块全部定为核心模块。

数据来源：各 subagent 草稿末尾的覆盖率明细表；主 agent 在阶段 7 的补充阅读已计入。

| 模块 | 类型 | 文件数 | 有效代码行 | 已读行数 | 覆盖率 | 达标 |
|---|---|---:|---:|---:|---:|---|
| M1 编排骨架 | 核心 | 1 | 865 | 865 | 100% | ✅ |
| M2 选择与装配 | 核心 | 3 | 744 | 744 | 100% | ✅ |
| M3 失败路径 | 核心 | 3 | 430 | 430 | 100% | ✅ |
| M4 可用性防护 | 核心 | 2 | 509 | 509 | 100% | ✅ |
| M5 计量 | 核心 | 2 | 312 | 312 | 100% | ✅ |

**合计：2860/2860 = 100% ✅**

## 逐文件明细

| 文件 | 总行数 | 已读 | 覆盖率 | 归属 |
|---|---:|---:|---:|---|
| `src/gateway/gateway.py` | 865 | 865 | 100% | M1 |
| `src/gateway/registry.py` | 312 | 312 | 100% | M2 |
| `src/gateway/rate_limit.py` | 284 | 284 | 100% | M4 |
| `src/gateway/router.py` | 246 | 246 | 100% | M2 |
| `src/gateway/health.py` | 225 | 225 | 100% | M4 |
| `src/gateway/types.py` | 186 | 186 | 100% | M2 |
| `src/gateway/retry.py` | 177 | 177 | 100% | M3 |
| `src/gateway/usage.py` | 161 | 161 | 100% | M5 |
| `src/gateway/cost.py` | 151 | 151 | 100% | M5 |
| `src/gateway/errors.py` | 147 | 147 | 100% | M3 |
| `src/gateway/fallback.py` | 106 | 106 | 100% | M3 |
| `src/gateway/__init__.py` | 0 | 0 | — | 空文件 |

## 主 agent 的补充阅读（不计入模块覆盖率）

| 材料 | 行数 | 用途 |
|---|---:|---|
| `docs/架构概要设计-gateway.md` | 480 | 一手设计意图、B-1…B-9 决策、§9.5 的 R-1…R-8 回填修订 |
| `docs/需求说明书-gateway.md` | 424 | FR-G-01…13 / NFR-G-01…08、B-1…B-16 验收点 |
| `tests/unit/gateway/test_acceptance.py` | 509 | 验收点覆盖强度抽查（发现 B-1 扫描范围窄、B-8 断言弱） |
| `tests/unit/gateway/test_budget.py` | 192 | `CallBudget` / `fallback.decide` 的直接单测 |
| `tests/unit/gateway/conftest.py` | 199 | 夹具与测试替身（发现 `FakeClock.sleep` 无 await 导致盲区） |
| `src/composition/bootstrap.py` | 148 | 装配接线、启动期校验的实际归属 |
| `src/provider/openai/client.py` | 部分 | 验证真实上界 `×(1+retries)`（跨模块结论） |
| `src/provider/openai/{llm,embedding}.py` | 部分 | 本会话修复的两个缺陷 + 实测校验 |
| `src/foundation/clock.py` | 部分 | 验证假时钟无取消点 |

## 抽查验证记录

每个核心模块抽 2-3 条关键结论回源码逐行核对，全部记录在 `07-cross-validation.md`：

| 模块 | 抽查条数 | 确认 | 修正/推翻 |
|---|---:|---:|---:|
| M1 编排 | 3 | 3 | 0 |
| M2 选择 | 4 | 4 | 0 |
| M3 失败 | 4 | 4 | 0 |
| M4 防护 | 1（与 M1/M3 冲突） | 0 | **1（推翻其「无泄漏」结论）** |
| M5 计量 | 4 | 4 | 0（另补充发现 1 条） |

**最关键的一次抽查**：M4 声称「熔断探测位在所有路径上都归还，13 条出口逐条核过」，
与 M1、M3 的结论直接冲突。回源码裁决为 **M1/M3 正确** ——
泄漏窗口在 `gateway.py:409` 的 `try` **之前**（`:377` 占位 → `:390` 可挂起的 `await` → `:408` 才上膛），
M4 的核查范围是 `try` **之内**的出口，属于作用域差导致的假阴性。
