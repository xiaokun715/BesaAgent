# 05 报告结构 + 模块叙事线

## 系统性设计哲学（贯穿全模块，所有 subagent 必须连接到它）

> **拒绝静默 —— 把不确定性压缩成可数、可读、可复现的东西。**

四个具体表现，它们是同一件事的四个面：

| 表现 | 落点 |
|---|---|
| 故障放大倍率**有上界**，且上界是**一个能读出来的数字** | `CallBudget` 单一计数点（`retry.py`） |
| 未知**不是** 0 | `Usage.output_tokens=None` / `Cost.amount=None`（`cost.py` `usage.py`） |
| 降级**必须标记** | `GatewayResult.degraded` + `attempts` 全保留（`types.py`） |
| 失败原因**全留**，不只留最后一条 | `AttemptRecord` 元组（`types.py`） |

反面即设计要消灭的东西：静默降级、静默错位、静默失真、静默失效。
报告所有「为什么这样设计」的结论，都应能回溯到这条哲学。

## 报告大纲（用户选择：精简开头，跳过竞品）

| 章 | 标题 | 内容 | 素材来源 |
|---|---|---|---|
| 1 | 项目全景：一次调用的旅程 | 一段场景（多 agent 并发跑回归的痛点）→ gateway 是什么 → 9 道关的责任链全景图 → 四条硬约束 | 主 agent + M1 草稿 |
| 2 | 选谁：逻辑名寻址与能力过滤 | registry（启动期校验、软失败）+ router（策略管道、health 排序偏好 vs 硬拦截）+ types（类型三件套 D-A） | M2 草稿 |
| 3 | 让不让打：限流与熔断 | rate_limit（RPM/TPM/并发三维、TPM 预扣回补、on_exceed 两种行为、R-8 的 `>=`）+ health（状态机、R-5 探测位归还、B-6 半开判据） | M4 草稿 |
| 4 | 打失败了怎么办：重试、降级与错误语义 | **CallBudget（全模块最重要机制）** + retry（重试不叠加）+ fallback（两条禁令、R-3 判定顺序、R-4 单候选上限）+ errors（归一化边界） | M3 草稿 |
| 5 | 记账：用量与成本 | usage（台账、交付方向 NFR-G-06）+ cost（缓存折扣、None vs 0、D-7 异步落库） | M5 草稿 |
| 6 | 回到骨架：把 9 道关缝成一个方法 | `gateway.py` 深潜：B-3 的证据、`_execute` vs `_stream_impl` 双路径、`_finalize`、B-4 事件只在门面、取消传播、R-6 degraded 语义 | M1 草稿 |
| 7 | 实测校验：设计承诺 vs 真实行为 | 6 条实测发现（见 `03-plan.md`）—— 哪些兑现、哪些是已知限制、哪些是差距 | 主 agent |
| 8 | 评价与启发 | 诚实优缺点、与通用做法的差异、如果重新设计 | 主 agent + 各草稿 |

附录：设计决策索引（B-1…B-9、R-1…R-8、D-*）、需求追溯（FR/NFR → 代码位置）、覆盖率汇总不放报告。

## 叙事线（每个模块的过渡逻辑）

```
第 1 章 全景
   │  「9 道关，但一次调用只能有一个编排者」——先立骨架，后面五个零件才有挂载点
   ↓
第 2 章 选谁（registry + router + types）
   │  编排的第一步是「候选链从哪来」。拿到有序候选链后，第一个要回答的问题不是
   │  「打哪个」，而是「这个候选现在值不值得打」
   ↓
第 3 章 让不让打（rate_limit + health）
   │  这两道关在 retry **之前**（硬约束①），因为它们决定的是「要不要发起」，
   │  而不是「发起了失败怎么办」。可一旦真的发出去并失败，就进入本模块最核心的问题
   ↓
第 4 章 打失败了怎么办（retry + fallback + errors）
   │  这里是全模块的高潮：CallBudget 用结构消除了「重试 × 候选」的乘积。
   │  失败路径会产生两样东西必须落账：token 用量与成本
   ↓
第 5 章 记账（usage + cost）
   │  计量是「拒绝静默」哲学最纯粹的表达。五件事都讲完了，回到开头那个问题：
   │  这 9 个零件是怎么被缝成「一次调用」的
   ↓
第 6 章 回到骨架（gateway.py 深潜）
   │  编排方法里藏着设计文档没写的部分：双路径（阻塞 vs 流式）为何必须分开、
   │  事件为何只在门面发、取消如何传播。设计承诺是否兑现，要用真实调用检验
   ↓
第 7 章 实测校验
   │
   ↓
第 8 章 评价与启发
```

## 强调「不按目录顺序组织」的理由

按目录顺序读（gateway → registry → router → retry → fallback → rate_limit → health → usage → cost）
会让读者在还不知道「为什么需要限流」时就先读限流实现。本报告按**一次调用的时间线**组织：
选谁 → 让不让打 → 打失败怎么办 → 记账 → 回到编排。

## Subagent 分工（阶段 6）

| Agent | 模块 | 文件 | 行数 | 产出 |
|---|---|---:|---:|---|
| A1 | M1 编排骨架 | `gateway.py` | 865 | `06-module-m1-orchestration.md` |
| A2 | M2 选择与装配 | `router.py` `registry.py` `types.py` | 744 | `06-module-m2-selection.md` |
| A3 | M3 失败路径 | `retry.py` `fallback.py` `errors.py` | 430 | `06-module-m3-failure.md` |
| A4 | M4 可用性防护 | `rate_limit.py` `health.py` | 509 | `06-module-m4-protection.md` |
| A5 | M5 计量 | `cost.py` `usage.py` | 312 | `06-module-m5-metering.md` |

## 主 agent 在等待期间的工作

1. 读 `tests/unit/gateway/*` 了解验收点的实际断言（**不与 subagent 抢源码文件**）
2. 读 `src/composition/bootstrap.py`（已读过）确认装配接线
3. 设计第 7 章实测校验的写法
4. 起草第 1 章全景与第 8 章评价的骨架

## 必须交叉验证的跨模块结论（标注【待主 agent 验证】）

1. 「`CallBudget.try_acquire()` 是唯一计数点」—— 需全模块检索确认没有第二个 attempt 计数器
   （设计文档 §10 把它列为最大风险）
2. 「事件只在 gateway.py 发」—— 需确认其余 10 个文件不发事件
3. 「`None` vs `0` 语义贯穿全模块」—— 需确认没有哪一处把未知写成了 0
4. 「provider 只被 gateway 引用」—— FR-G-01 验收点 B-1，需全仓库检索
