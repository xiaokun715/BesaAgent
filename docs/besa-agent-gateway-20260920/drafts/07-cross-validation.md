# 07 交叉验证（主 agent）

## A. 四项跨模块结论的机械校验

规划里标注【待主 agent 验证】的四条，全部用全仓库检索核实。

### A-1 「`CallBudget.try_acquire()` 是唯一计数点」—— ✅ 成立

设计文档 §10 把「`CallBudget` 被绕过」列为**头号风险**（「上界失效，故障放大」），
并留下一句强约束：「**不允许任何地方出现第二个 attempt 计数器**」。

全模块检索 `+= 1`，只有 6 处：

| 位置 | 是什么 | 是否 violate |
|---|---|---|
| `src/gateway/retry.py:98` | `self._used += 1`，在 `try_acquire()` 内 | **就是那唯一一处** |
| `src/gateway/gateway.py:476` | `retry_index += 1` | ❌ 不是预算计数器，是**单候选内**的重试序号（R-4 要求的「单候选上限」判据） |
| `src/gateway/health.py:114/133/149` | 探测位、探测成功数、连续失败数 | ❌ 熔断状态机自己的度量，不参与上游调用配额 |
| `src/gateway/rate_limit.py:199` | `self._inflight[key] += 1` | ❌ 并发信号量计数 |

**结论**：全局配额计数器**确实只有一处**。值得在报告里点明的是 `gateway.py:476` 那处 ——
它是一次**看起来像违反、实际不是**的检索命中：`retry_index` 服务于 `max_attempts_per_candidate`
这个**单候选**上限，与 `CallBudget` 的**跨候选累计**上限是两条不同的约束（R-4 明确要求两者同时判）。
「有两个上限但只有一个计数器」这个区分，正是这个设计容易被误读的地方。

### A-2 「事件只在门面发」—— ✅ 成立（B-4）

`.emit(` 在全模块只出现 2 次定义 + **1 次调用**：

- `src/gateway/gateway.py:88` / `:97` —— `EventEmitter` Protocol 与 `NullEmitter` 的方法定义
- `src/gateway/gateway.py:837` —— **唯一的调用点**，在 `Gateway._emit` 辅助方法内

其余 10 个文件里 `retry` / `health` / `rate_limit` 等子模块都不发事件，只**返回**发生了什么。
B-4 的意图（「否则事件语义会散在 9 个文件里，且难以保证顺序与完整性」）在结构上成立。

### A-3 「provider 只被 gateway 引用」—— ❌ **发现真实违反**

`FR-G-01` 的验收点 B-1 写的判据是：「全仓库检索，除 `src/gateway/` 外**无任何模块**直接 import `src/provider/`」。

实测：

```
apps/cli/commands/chat.py:17:from provider.types import Message
```

**CLI 直接 import 了 `provider.types.Message`。**

两点补充，避免把它说得比实际更严重：

1. **机械契约抓不到它**：`pyproject.toml` 的 import-linter `root_packages` 只列了 `src` 下的顶层模块，
   `apps/` 不在其中。所以这条违反不会被 CI 拦住 —— 契约覆盖的是一个**比 FR-G-01 更小的范围**。
   （该文件当前处于作者的改名中途，`root_packages` 还写着已不存在的 `chat`，**四条契约现在根本没在跑**，
   这是另一件独立的事。）
2. **它是被签名逼出来的**：`Gateway.chat()` 的形参是 `messages: Sequence[Message]`
   （`src/gateway/gateway.py:139`），`Message` 是 `provider.types` 里的类型。任何调用方要**构造**对话历史，
   就必须拿到这个类型。CLI 是当前唯一的调用方，所以它第一个撞上。
   这是我本会话早前报告过的边界缺口 —— 契约禁止 `src` 业务模块 import provider，
   但 gateway 的公开签名又要求传入 provider 类型，两者不可能同时成立。

**报告的落点**：这属于「设计承诺 vs 实现现实」的一手材料，与 R-1…R-8 同类，但**文档没有回填这一条**。
建议在报告的评价章节给出三种出路（gateway 再导出 / 放宽契约到允许 `provider.types` / 各层自建 DTO + 边界转换），
并指出「再导出」是唯一不动契约文件的方案。

### A-4 「`None` vs `0` 语义贯穿全模块」—— ✅ 成立，但有一处**单侧未知**的边界

反查「有没有把未知写成 0」：全模块 `= 0` / `or 0` 共 20 处，逐个归类后只有 **3 处落在计量域**：

| 位置 | 代码 | 判定 |
|---|---|---|
| `src/gateway/cost.py:120` | `input_tokens = usage.input_tokens or 0` | ⚠️ 见下 |
| `src/gateway/cost.py:121` | `output_tokens = usage.output_tokens or 0` | ⚠️ 见下 |
| `src/gateway/cost.py:122` | `cached = usage.cached_input_tokens or 0` | ✅ 正确（缓存未命中就是 0 条命中，0 是精确值） |

其余 17 处都是**计数器与默认值**（熔断连续失败数、探测位、预算已用数、重试序号、优先级默认值），
与「未知」语义无关。

`src/gateway/cost.py:103` 的守卫是 `input_tokens is None **and** output_tokens is None` ——
**只有两侧都未知才算未知**；`or 0` 兜住的是**单侧未知**。

于是存在这样一个边界：**若某厂商只返回 `completion_tokens` 而不返回 `prompt_tokens`，
成本会按「输入 0 token」静默少算**，而不是标记为未知。

这与本会话的实测结果一致且互补：embedding 场景下 `output_tokens` **恒为 `None`**
（provider 侧 `_parse_usage` 刻意如此），而成本仍算得出来（`0.0000023 CNY`）——
说明「单侧未知按 0 计入」在 embedding 这个场景下**恰好是对的**（向量化本来就没有输出 token），
但它把「这个场景恰好对」与「未知应如实标记」这两件事绑在了一起。

全模块共 26 处显式 `is None` 判定，说明这条纪律是被主动执行的。
**结论：纪律成立；`cost.py:120-121` 是一处有意的工程折中，代价是「部分未知」无法表达。**
（具体是缺陷还是合理取舍，待 M5 草稿的独立结论，报告里合并。）

---

## B. 验收点测试的覆盖强度抽查（主 agent 独有视角）

900 行单测覆盖了 B-1…B-16 **全部 16 个验收点**，另有 5 个需求清单之外的用例
（`test_budget_exhausted_reports_reason`、`test_b10b_stream_before_first_chunk_can_fallback`、
`test_unknown_usage_is_not_zero`、`test_event_emitter_failure_does_not_break_calls`、
以及 `test_budget.py` 里 16 条对 `CallBudget` / `fallback.decide` 的直接单测）。
覆盖密度是够的。但抽查发现**两个验收点的测试断言比需求更弱**，且都指向真实的实现边界：

### B-1 的测试范围比需求窄 —— 所以 A-3 的违反抓不到

`tests/unit/gateway/test_acceptance.py:50`：

```python
src = Path(__file__).resolve().parents[3] / "src"
```

测试只扫描 **`src/` 目录**，而 `FR-G-01` 的判据是「**全仓库**检索，除 `src/gateway/` 外无任何模块直接 import provider」。
`apps/cli/commands/chat.py:17` 的 `from provider.types import Message` 落在这个测试的扫描范围之外，
**测试是绿的**。

这不是「测试写错了」——测试的注释解释了为什么用源码扫描而非运行时断言（「运行时断言只能证明这次调用没走别处，
证明不了别处根本没有这条路」），这个理由本身很对。问题是**扫描边界**与需求措辞不一致，
而需求措辞（「全仓库」）才是契约的本意。于是出现一个结构性盲区：
`src/` 里守住了，`apps/` 里没有，而**唯一会用到 gateway 的代码就在 `apps/`**。

**报告落点**：与 A-3 合并成一条 —— 「唯一入口」这条约束在 `src/` 内被机械保证了，在 `apps/` 内没有。

### B-8 断言的是「退避等待受限」，不是「在途请求被取消」

`tests/unit/gateway/test_acceptance.py:210-230`，测试名 `test_b8_total_deadline_is_respected`，
核心断言只有一行：

```python
assert gateway._clock.total_slept <= 10.0
```

它测的是**退避 sleep 的总和**不超 deadline。测试自己的 docstring 也如实说了这一点：
「这条测的是『退避等待也受预算约束』—— 不判这一条，`1 + 2 + 4 + 8 …` 的指数退避能把 10 秒撑成好几分钟。」

但 `FR-G-12` 的原文是：

> 预算耗尽时**立即停止**并报「超时」，**不得**再发起新的尝试

**「立即停止」没有被任何测试验证**，而本会话的真实 API 实测正好打在这个空档上：
`deadline_s=0.001` 时单候选调用仍跑满 **0.56s**（一个在途 HTTP 请求不会被预算中断）。
两个候选时第 2 跳确实被拦下并抛 `BudgetExhaustedError（deadline）` —— 说明预算机制**本身是对的**，
它约束的是「**要不要发起下一次**」，不约束「**已经在途的那一次**」。

**这是一个精确的边界，不是缺陷**：要中断在途请求，必须把 `timeout_s` 也纳入预算
（形如 `timeout_s = min(cfg.timeout_s, budget.remaining_s())`），代价是每次调用前都要重算超时、
且「单次请求可以跑多久」这件事从模型配置变成了运行时决定。
但**测试名与需求原文都没有表达出这个边界**，读者容易以为 FR-G-12 已被完整覆盖。
报告应在第 7 章把这条差距写清楚，并明确它属于「文档与测试的措辞比实现承诺得更多」。

### 一处值得称赞的测试注释

`tests/unit/gateway/test_acceptance.py:261-262` 在 B-9 用例里写了：

> 3 个候选只试到第 2 个预算就没了 → 这是**真的**「还有候选没试过」，报预算耗尽。
> 与「候选都试完且都失败」（`AllCandidatesFailedError`）是不同的问题。

这是 R-3（`fallback.decide` 判定顺序）在测试里的直接落地，而且把「两种失败为什么必须区分」写进了断言旁边 ——
属于那种**读代码读不出来、但作者一定知道**的细节。M3 草稿如果没提这一点，报告融合时补上。

---

## C. M1（编排骨架）关键结论抽查 —— 三条全部**回源码独立确认**

### C-1 「熔断探测位泄漏窗口」—— ✅ 确认，且**在测试里物理不可达**

草稿的 P0 结论。我独立读源码复核：

```
gateway.py:377   self._health.allow(spec.key)        ← HALF_OPEN 下占一个探测位
gateway.py:390   await self._limiter.acquire(...)    ← 真正的挂起点
gateway.py:393   self._health.release(spec.key)      ← 只在 decision.denied 分支走到
gateway.py:408   probe_outstanding = True            ← 上膛在 await 之后
gateway.py:409   try:                                ← try 块也在 await 之后
```

`:393` 的归还**只覆盖 `denied` 这一条出路**。若 `acquire` 在等待期间抛 `CancelledError`（调用方取消）
或任何异常，控制流既不经过 `:393`、也还没进 `:409` 的 `try` → **探测位永久泄漏**。
后果与 R-5 描述的一字不差：`HALF_OPEN` 探测位被占死，模型再也回不到 `CLOSED`，
而日志上只有一条「进入半开探测」。

**更关键的是为什么测试抓不到**（草稿这半我原本存疑，核实后成立）：

| | 实现 | 是否可被取消 |
|---|---|---|
| 生产 | `SystemClock.sleep`（`foundation/clock.py:65-69`）→ `await asyncio.sleep(seconds)` | ✅ 真挂起，可取消 |
| 测试 | `FakeClock.sleep`（`foundation/clock.py:108-114`）→ 只 `sleeps.append` + `advance`，**无 await** | ❌ 不会挂起，无取消点 |

于是 `test_b15_cancellation_releases_quota` 测的是**另一条**路径（上游调用内部的取消），
它覆盖得很好，但泄漏窗口所在的限流等待期间在假时钟下**根本没有挂起点**。
「假时钟让时间快进」这个测试便利，恰好把一类并发缺陷变成了不可测。

**这条值得进报告的评价章节**：它不是「忘了写 finally」这种低级错误，
而是**测试替身与真实实现在「挂起点」这个维度上不等价**造成的盲区 —— 一个更普遍的教训。

### C-2 「流式路径不记失败用量」—— ✅ 确认

```
$ grep -rn "_record_failure_usage" src/gateway/*.py
src/gateway/gateway.py:449     ← 唯一的调用点，位于阻塞路径 _attempt_candidate
src/gateway/gateway.py:764     ← 定义
```

而设计文档给这条约束写的理由点名了流式场景（`架构概要设计-gateway.md:129`）：
「失败的调用也可能产生用量 —— 上游可能已计费（**尤其是流式半截断开**），漏记会导致账单对不上」。
**被点名的场景恰恰是没实现的那一个。**

### C-3 「`CIRCUIT_OPENED` 定义但零发射」—— ✅ 确认

```
$ grep -rn "CIRCUIT_OPENED" src/gateway/gateway.py
src/gateway/gateway.py:81   CIRCUIT_OPENED = "gateway.circuit.opened"    ← 只有定义
```

`_emit` 的全部调用点是 `:302 / :341 / :386 / :402 / :450 / :477 / :742`，无一处发 `CIRCUIT_OPENED`。
而 `FR-G-10` 明确点名的 7 类事件里就有「熔断状态变更」。
（另：`_stream_impl` 里一个 `_emit` 都没有 —— 流式失败时事件流里什么都没有。）

---

## D. M5（计量）关键结论抽查 —— 四条全部确认，另发现一处同源缺口

### D-1 「`dropped` 是半个交付」—— ✅ 确认

```
$ grep -rn "dropped" --include=*.py src apps tests | grep -v "^src/gateway/usage.py"
（无输出）
```

全仓库**零处读取** `dropped`。计数器存在于 `usage.py`，但 `drain()` 只返回
`tuple[UsageRecord, ...]`，计数器**不在交付物里**。也就是说：丢了数据，
而唯一知道丢了多少的地方**没有任何出口**。

这与本模块的核心哲学正面冲突 —— 「拒绝静默」的四个面里，这条恰恰是**静默丢数据**。

### D-2 「`drain()` 零调用方，所以现在必然丢账」—— ✅ 确认

```
$ grep -rn "\.drain()" --include=*.py src apps tests | grep -v "def drain"
（无输出）
```

`src/repo/` 7 个文件全为 0 字节。所以「gateway 产生数据、组合根接线」这个设计
（`NFR-G-06`）**目前只完成了产生的一侧**。草稿的量化估算值得采信：
一次回归约 7200 条记录（成功 1 条 + 每次失败尝试 1 条），单轮就能摸到 `max_records=10_000` 上限，
之后进入「恒定 1 万条 + `dropped` 单调爬升」的稳态 —— 而排障时看到的「未交付的用量记录：10000 条」
**看起来完全正常**。

### D-3 「`cost.py` / `usage.py` 无任何直接单测」—— ✅ 确认

```
$ find tests -name "*cost*" -o -name "*usage*" -o -name "*metering*" | grep -v __pycache__
（无输出）
```

两文件 312 行，只有 B-12 / B-13 两条**集成路径**覆盖到。本章讨论的每个争议点
（缓存扣减、`min` 钳制、单侧未知、`_add_opt` 真值表、`dropped`、混合 `None` 汇总）
**都没有回归保护**。这对一个「以 None 语义为核心纪律」的模块是明显的不对称 ——
纪律靠人记，不靠测试守。

### D-4 「失败记录的 `attempt_index` 恒为 0」—— ✅ 确认，且**还有一处更严重的同源缺口**

`gateway.py:764-788` 的 `_record_failure_usage` 构造 `UsageRecord` 时不传 `attempt_index`
（字段默认 0，`usage.py:49`），而 `retry_index` 就在手边（`gateway.py:410`）。
`usage.py` 里这个字段的注释写着「重试会重复计费，这个字段让账单可解释」——
**最需要解释的失败路径上它恒为 0**。

**我额外发现（草稿未提到）**：同一个构造函数里 `alias=""` 是硬编码的空串。而
`usage.py:135` 的 `totals(alias=...)` 是按 `item.alias != alias` 过滤的 ——

> 任何按逻辑名做的用量聚合，**会静默丢掉全部失败记录**。

`FR-G-08` 要求「维度至少支持按模型、按**逻辑名**、按会话、按调用方标识」，失败记录恰好把
逻辑名维度丢空了。这条比 `attempt_index` 更严重，因为它直接削弱需求点名的聚合能力。

（顺带核实：`cost.py` 的行号在**本会话早前我修那段 docstring 时整体下移了约 12 行**，
报告引用行号时以当前磁盘为准 —— 早前的笔记用的是修改前的行号。）

---

## E. M2（选择与装配）关键结论抽查 —— 四条确认，两条待 M4 返回后补

### E-1 「启动期校验根本不在组合根」—— ✅ 确认，B-9 是**文档缺陷**

草稿判定「文档说 A、实现是 B」，并主张 B-9 的理由本身不成立。核实：

| 校验 | 位置 | 归属 |
|---|---|---|
| alias 至少一个候选 / 悬空候选 / 未知策略名 | `registry.py:129-142`（`validate()`） | registry |
| 候选的 provider 已注册 | `registry.py:242-247`（`_build_spec`，查 `known_providers()`） | registry |
| `total_max_attempts ≥ max_attempts_per_candidate` | `retry.py:129-140`，由 `bootstrap.py:104` 触发 | retry |

`bootstrap.py` 自己**一条校验都没写** —— 它只做「把 `Settings` 拆成 gateway/models/providers 三段」的投影与顺序。

B-9 写的理由是「第 5 条校验需要同时看见 `settings` 与 `src/provider`，只有组合根两边都看得见」。
而 `registry.py` 从**第一行 import 起就 import 了 provider**（它必须 import，因为要构造 provider 实例）——
理由不成立。**判定：文档缺陷，不是代码缺陷。** 真实的分工原则应表述为
「**谁拥有该不变式，谁校验它**」：registry 拥有「候选必须指向已注册厂商」，retry 拥有「上限之间的大小关系」。

报告落点：与 R-1…R-8 并列，作为「第 9 条实现期修订」——文档没回填。

### E-2 「`router.py:231` 是逻辑不可达的死代码」—— ✅ 确认

```python
# router.py:231
if unavailable and not candidates:
    lines.append("（候选集为空：请检查 alias 的 candidates 是否引用了已注册的模型）")
```

`unavailable = [spec for spec in candidates if not spec.available]`（`router.py:191`），
即 `unavailable ⊆ candidates`。要进这个分支需要「`unavailable` 非空 **且** `candidates` 为空」——
后者成立时前者必然为空。**条件恒假，这行提示永远不会出现。**

讽刺之处在于行内文字正是《需求说明书》`C-3` 想要的「能直接照着改配置」的提示，
而它恰好在最需要它的场景（候选集为空）下不可达。

### E-3 「能力名混排 + 改名遗留污染用户可见信息」—— ✅ 确认，且有**实测证据**

`router.py:225` 是 `missing = sorted(cap.value for cap in (ctx.required - spec.capabilities))` ——
消息里渲染的是 `Capability` 的**枚举值**，而 `Capability.CHAT` 的值已被改名成 `"runtime"`。

本会话早前的**真实调用**打出了完全一致的输出：

```
NoCapableModelError: 没有任何候选满足本次请求的能力要求（对话）
  - emb-siliconflow：缺少 runtime
```

一个**对话**请求报「缺少 runtime」—— 消息同时混用了中文描述（「对话」）与英文枚举值（`runtime`、`tools`），
且 `runtime` 这个词对用户不表达任何信息。这是那次半途改名的**用户可见后果**，
比 `configs/` 崩掉更隐蔽（配置错会炸，消息错不会）。

### E-4 「后写的策略是主键」—— ✅ 确认（文档零字提及的陷阱）

`router.py:194-199` 按配置顺序逐个应用策略，而每个策略都是 `sorted()`（稳定排序）：

```
_by_weight  router.py:122   sorted(..., reverse=True)
_by_cost    router.py:137   sorted(...)
_by_health  router.py:155   sorted(...)
```

稳定排序的语义决定了**最后应用的那个策略成为主键**，先前的只作 tie-breaker。
于是 `strategy: [priority, cost]` 实际是「按成本排，优先级只在同价时生效」——
与配置作者的直觉（先写的优先）**正好相反**。需求 `FR-G-03` 只说「策略可组合且**顺序明确**」，
没有定义这个顺序的语义方向，文档也零字提及。

**报告落点**：归入「有意的设计，但缺一句文档」类问题。

### E-5 待补（依赖 M4 的 `health.py`）

草稿称 `_by_health`（`router.py:155`）里的 `ctx.health.breaker(spec.key)` 会 **get-or-create** 熔断器，
即**读操作带写副作用**，打破「router 是纯函数」的假设。`breaker()` 的实现属 `health.py`（M4 分析中），
**这条等到 M4 返回后验证**。

### E-6 一处值得表扬的设计（我在核实时顺手读到的）

`src/gateway/router.py:126-127` 的 `_by_cost` docstring：

> 便宜的优先。**价格未知的排最后**，而不是当成 0 排最前 ——
> 后者会让「没配价格」的模型独占流量，然后在账单上给你一个「未知成本」。

这是「拒绝静默」哲学在路由层的一个漂亮变体：**同一个 `None`，在排序里必须当成「最差」而不是「最好」**，
否则配置缺失会静默地改变流量分配。报告应引用它作为「哲学贯穿到每个文件」的例证。

---

## F. 其余模块（M3 失败路径 / M4 可用性防护）关键结论抽查（待返回）

## F. M3（失败路径）+ M4（可用性防护）关键结论抽查

### ⚠️ F-0 独立结论冲突的裁决（本轮交叉验证最重要的产出）

**M1 与 M3 各自独立报告了一个「熔断探测位泄漏窗口」；M4 声称「`release()` 在所有路径上都归还了，
13 条出口逐条核过，没找到泄漏」。三者对我同一份代码给出了相反结论。**

回源码裁决 —— **M1/M3 正确，M4 的结论在模块层面不成立**：

```
gateway.py:377   if not self._health.allow(spec.key):    ← HALF_OPEN 下占探测位
gateway.py:390   await self._limiter.acquire(...)        ← 真挂起点，见下
gateway.py:391   if decision.denied:                     ← 只有这一支归还
gateway.py:393       self._health.release(spec.key)
gateway.py:408   probe_outstanding = True                ← 上膛在 await 之后
gateway.py:409   try:                                    ← try 块也在 await 之后
gateway.py:495   finally:  → :498 release                ← 只覆盖 try 之后的路径
```

`rate_limit.py:166` 确有一处**真** `await self._clock.sleep(wait_s)`（在 `on_exceed="wait"` 且 RPM/TPM 超限时进入）。
所以：**取消发生在限流排队期间 → `CancelledError` 从 `:390` 抛出 → `:393` 的归还（在 `denied` 分支内）
与 `:409` 的 `try/finally` 都还没到 → 探测位永久泄漏。**

**M4 为什么漏了**：它核的是 `gateway.py:495` 与 `:661` 两个 `finally` **之内**的全部出口（那 13 条），
结论在「`try` 之内」这个范围内是对的。**但泄漏窗口在 `try` 之前** —— 这是一个**作用域差**导致的假阴性，
不是粗心。报告应把这次冲突本身写进去：三个独立分析对同一段代码得出相反结论，
而分界点只是「你从哪一行开始数出口」。

**这条也解释了为什么测试抓不到**（M1 提出、我复核成立）：
`FakeClock.sleep`（`foundation/clock.py:108-114`）只 `sleeps.append` + `advance`，**没有 await** ——
`await` 一个不会挂起的协程**不产生取消点**。于是这个窗口在假时钟下物理不可达，
而 `test_b15_cancellation_releases_quota` 测的是上游调用内部那条覆盖良好的路径。

**结论**：真缺陷，且生产必现、测试必不现。修法两行，但**不能简单把 `try` 提前** ——
M3 指出限流器的 `release()` 是「谁在途减谁」（`rate_limit.py:203-206`），提前会替别的并发请求释放额度。

### F-1 「真实上界是 8 而不是 4」—— ✅ 确认，**这条挑战设计的旗舰承诺**

M3 的核心发现。核实 `src/provider/openai/client.py:170-178`：

```
client.py:170   if attempt >= self._retries: raise last
client.py:177   await self._clock.sleep(delay)
client.py:178   attempt += 1
```

`retries` 默认 1（`provider/types.py:354`、`configs/base.yaml:81`）→ `attempt` 走 0→1，
即**每次 provider 调用产生 2 次 HTTP 尝试**（1 首发 + 1 重试）。

于是真实上界是：

```
total_max_attempts(4) × (1 + provider.retries(1)) = 8
```

**设计文档 §0 的原话是**（`架构概要设计-gateway.md:14-15`）：

> 让「故障」在系统里的放大倍率**有上界**，且这个上界是一个**可以被读出来的数字**，
> 而不是散落在各处的重试次数相乘出来的意外结果。

**「有上界」仍然成立（8 是上界）；「是一个可以被读出来的数字」不成立** ——
这个 8 是 gateway 侧配置（`configs/base.yaml:142`）与 provider 侧配置（`configs/base.yaml:81`）
的**乘积**，全仓库没有任何一处能读出来。而设计文档恰恰用「不是散落在各处的重试次数相乘出来的意外结果」
来描述要消灭的东西 —— **它的形态与设计要消灭的那个东西一模一样**，只是量级从 9/27 降到了 8。

同一因子还污染了限流账本：provider 的内部重试**不重新进入限流器**，
所以实际 RPM 是配置值的 `(1+retries)` 倍。

**报告落点**：这是全篇最有力的一处「设计承诺 vs 实现现实」，因为它不是某个分支写漏了，
而是**两个模块的配置各自正确、乘起来违背了本模块的第一目标**。
修法有三条（把 provider 重试折进 `CallBudget` / 在上界计算里显式乘出来并放进 doctor / 取消 provider 层重试），
报告会给出取舍。

### F-2 「`on_stream_started` 只写不读」—— ✅ 确认

```
$ grep -rn "on_stream_started" --include=*.py src apps tests
src/gateway/fallback.py:25   on_stream_started: bool = False        ← 字段定义
src/gateway/fallback.py:32   on_stream_started=bool(data.get(...))  ← 从配置读入
```

**零处读取。** 配置项 `fallback.on_stream_started`（也出现在 `configs/base.yaml`）是一个
**配置了没有任何效果**的开关。`decide` 的三个调用点全部硬编码 `stream_committed=False`，
流式边界实际由调用方的 `not chunks`（`gateway.py:605`）保证 —— 规则只在单测里生效。

### F-3 「`decide` 的 `error` 参数从未被读取」—— ✅ 确认

`fallback.py:84-90` 在 docstring 里把它列为参数（`error: 当前候选的失败。`），
但函数体（`:91-106`）只用到 `stream_committed` / `policy.enabled` / `remaining_candidates` /
`budget.exhausted_reason()`。`:103` 的注释「这里不看 `error.retryable`」是对的，
但**整个 `error` 参数都没被用** —— 而 `gateway.py:860-865` 的 `_last_error()` 专门为它构造了一个 `RuntimeError`。
两侧都以为对方在用。

**后果（M3 的推论，我认同）**：`ContextLengthError` 因此照常走降级 ——
烧光预算后报「所有候选均失败」，而真实修法是**压缩上下文**，两者方向相反。

### F-4 「禁止跨能力降级是「结构排除」」—— ⚠️ 结论过强，M3 的质疑成立

`fallback.py:78-82` 的原话：

> **跨能力降级不作为一条规则，因为它已被结构排除**：
> `router.select()` 在选链时就把不满足能力的候选全部过滤掉了……在结构上不可能发生。

核实：这个推理**依赖 `capability` 出现在 alias 的 `strategy` 列表里**，
而 `Registry.validate()`（`registry.py:137-142`）**只校验策略名是否已知，不校验 `capability` 是否在场**。
一份 `strategy: [priority]` 就能通过全部启动期校验，并让这条「结构排除」失效。

**但严重性要精确**：失效后的真实后果分两种情况 ——
- tools / vision / stream：`provider/base.py:95-104` 的 `guard_request` 会**硬拦截**并抛
  `CapabilityNotSupportedError`，不是静默错结果；
- **JSON：`guard_request` 是「降级而非拦截」**（`provider/base.py:99-104`，
  结果「仍然可用，只是约束弱了」）→ 这才是真正「看起来正常的错误结果」。

所以 `fallback.py` 的措辞应从「结构上不可能发生」改为
「**在 `capability` 策略启用的前提下**不可能发生，而该前提未被启动期校验强制」。

### F-5 M4 的三条独立发现（我抽样核实，全部成立且不与上述冲突）

- **`reconcile` 零调用点**：TPM 实际运行在**纯估算**模式，「按预估预扣、按实际回补」只实现了前半句。
  而估算（`_CHARACTERS_PER_TOKEN = 4`）在中文、输出 token、tools 定义三处**全部低估** → 约束系统性偏松。
- **`CIRCUIT_OPENED` 零发射的根因是结构冲突**：B-4 规定事件只在门面发，
  而 `record_failure/success` 返回 `None` → 门面**无法得知**状态是否变更。
  这个解释比 M1 的「定义了没发」更到位：**是 B-4 与 FR-G-10 两条约束的冲突，不是疏忽**。
- **D-8 的「模型 + 会话两层」实际是零层差异化**：`build_rate_limiter` 只读 `defaults`、
  构造单一策略，会话维度完全不存在 → 一个跑飞的 agent 可以合法吃掉整个模型配额，
  把同轮回归里其他 agent 饿死。**不是超发，是内部独占。**

---

## G. 全局关联验证

五个模块的草稿都连接到了「拒绝静默」这条主线，但连接方式各是一个**不同的面**，这是设计哲学
真正贯穿（而非被贴标签）的证据：

| 模块 | 「拒绝静默」在它的具体形态 |
|---|---|
| M2 选择 | `_by_cost` 里**价格未知排最后而不是当 0 排最前** —— 同一个 `None`，在排序里必须当成最差（`router.py:126-127`） |
| M4 防护 | 配了 redis backend 就**明确报错**，不静默退化成单进程（多进程下会超发 N 倍，而本地测试完全正常） |
| M3 失败 | `AttemptRecord` **全留**而非只留最后一条；`exhausted_reason` 区分 `attempts`/`deadline` 让修法方向可分 |
| M5 计量 | 「未知不是 0」的原始出处 |
| M1 编排 | `degraded` 只表示运行时故障，缺密钥不计入 —— 否则这个标记再也不能回答「这次是不是出了故障」 |

而**全篇最重要的发现恰恰是这条主线的七处破口**：探测位泄漏（静默）、`dropped` 无出口（静默）、
`alias=""` 丢失败记录（静默）、`on_stream_started` 只写不读（静默）、`CIRCUIT_OPENED` 零发射（静默）、
`reconcile` 零调用（静默）、provider 重试因子不可读（静默）。

**这个对照本身就是报告的核心论点**：一个把「拒绝静默」当作第一原则的项目，
它的破口全部集中在**「跨模块接缝」**上 —— 每个模块内部都守住了，缝的地方没有人的名字。



