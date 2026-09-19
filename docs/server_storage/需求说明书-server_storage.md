# `apps/server/storage` 需求说明书

| 项 | 值 |
|---|---|
| 文档层级 | **需求层**（第一篇）。后续：《`apps/server/storage` 架构概要设计》 |
| 覆盖范围 | `apps/server/storage/postgres/`、`apps/server/storage/redis/`，以及 `apps/cli/storage/` 的对照设计 |
| 上游依赖 | `foundation/db.py`（Base 与引擎工厂）、`src/repo/base.py`（`Database` / `Transaction` 协议） |
| 下游消费 | 组合根 `apps/*/runtime/`、`src/gateway`（限流后端）、`src/repo`（执行入口与向量实现） |
| 后端 | PostgreSQL 17.11 + pgvector 0.8.6、Redis 5.0.14.1 |
| 状态 | 待评审 |

---

## 1. 这份文档回答什么

只回答**要解决什么问题**、**必须提供哪些能力**、**什么算合格**。

**本文不回答**：连接池的具体参数、限流桶的 Lua 脚本、锁的续期算法。文中文件名仅用于圈定职责边界。

**前置阅读**：`docs/repo/需求说明书-repo.md`（本模块向它提供执行入口）、
`docs/gateway/需求说明书-gateway.md` `FR-G-06`（多进程限流）。

---

## 2. 背景与定位

### 2.1 为什么这些代码不能放在 `src/repo`

`src/repo` 被刻意设计成**存储无关**的（`NFR-R-01`）：

- 它**不建引擎** —— `foundation/db.py` 定死了「建引擎的权利只在组合根手里」；
- 它**不认识 Redis** —— 缓存/锁/限流不属于「权威数据」；
- 它只认两个协议：`Database`（事务边界）与 `Transaction`（事务内执行）。

于是「**具体怎么连**」这件事必须有人做，而它属于**进程启动的职责** ——
进程边界在 `apps/`，所以它落在 `apps/*/storage/`。

### 2.2 三个 app 需要的东西不一样

这是 `apps/*/storage/` 存在的第二个理由，也是它**不能**被合并成一个共享模块的原因：

| app | Postgres | Redis | 为什么 |
|---|---|---|---|
| `apps/server` | ✅ 必须 | ✅ 必须 | 多进程部署；`FR-G-06` 要求限额**跨进程生效** |
| `apps/cli` | ⚪ 可选 | ❌ | 单进程、短生命周期；默认不持久化（内存实现），需要时接本地 PG |
| `apps/mcp` | ⚪ 可选 | ⚪ | 取决于它是否常驻 |

`apps/cli/storage/` 与 `apps/server/storage/` 是**目录并列**的 —— 这个并列关系本身
就是在说「同一个 app 可以有不同的存储策略」。

### 2.3 `FR-G-06` 把一个缺口指名给了这里

`src/gateway/rate_limit.py` 当前对 `backend: redis` 的处理是**直接报错**：

> 静默退化成单进程限流，在本地测试下**完全正常**，但在多进程部署下实际配额会超发 **N 倍**（N = 进程数）。
> 这是一个**只在生产暴露**的失效形态，而且失效后没有任何症状 —— 配额看起来配了，就是不生效。

那个报错是**对的**（`拒绝静默`），但它同时意味着：**多进程限流这件事现在做不了**。
`apps/server/storage/redis/` 就是让那个报错不再需要存在的实现。

---

## 3. 范围

### 3.1 本模块负责

| 职责 | 落点 |
|---|---|
| **Postgres 装配**：引擎、连接池、健康检查 | `apps/server/storage/postgres/engine.py` |
| **执行入口的构造**：把引擎包成 `Database` 实现，交给组合根 | `apps/server/storage/postgres/database.py` |
| **pgvector 实现**：向量表模型、HNSW 索引、距离检索 | `apps/server/storage/postgres/vector.py` |
| **Redis 装配**：客户端、连接、健康检查 | `apps/server/storage/redis/client.py` |
| **多进程限流后端**（`FR-G-06`） | `apps/server/storage/redis/rate_limit.py` |
| **分布式锁**（断点续跑的互斥） | `apps/server/storage/redis/lock.py` |
| **缓存**（可丢失的读缓存） | `apps/server/storage/redis/cache.py` |
| **跨进程事件分发**（可选） | `apps/server/storage/redis/events.py` |
| **CLI 的存储策略**：默认内存、可选 PG | `apps/cli/storage/` |

### 3.2 本模块明确不负责

| 不做的事 | 归属 |
|---|---|
| 事务边界与仓储 | `src/repo` |
| 表的业务语义（会话/消息/事件/记忆…） | `src/repo` |
| 用量与成本的落库编排 | `src/repo/usage.py` |
| 限流的**决策**（该不该等、能不能换候选） | `src/gateway`（本模块只提供**计数与判定**的后端） |
| 事件总线的语义 | `src/event`（本模块只提供跨进程的传输） |
| 向量化（调模型） | `src/gateway` |
| 组合（把 repo / gateway / event 接到一起） | 组合根 `apps/*/runtime/` |

> **边界判据**：本模块回答「**怎么连上这个后端**」与「**只有这个后端才能做的事**」。
> 一旦问题变成「这个数据是什么」或「这个操作该不该做」，就不属于这里。

---

## 4. 数据与连接的全景

```mermaid
flowchart TB
    subgraph APPS["apps/"]
        subgraph SRV["apps/server/storage"]
            PG["postgres/<br/>engine · pool · Database<br/>PgVectorStore"]
            RD["redis/<br/>client · rate_limit · lock<br/>cache · events"]
        end
        CLI["apps/cli/storage/<br/>内存实现（默认）"]
    end

    subgraph SRC["src/"]
        REPO["src/repo<br/>Database / Transaction 协议"]
        GW["src/gateway<br/>RateLimiter 协议"]
        EV["src/event"]
    end

    ROOT["组合根<br/>apps/*/runtime/"]

    PG -->|"实现 Database 协议"| REPO
    PG -->|"实现 VectorStore 协议"| REPO
    RD -->|"实现限流后端协议"| GW
    RD -.跨进程传输.-> EV
    CLI -->|"实现 Database 协议"| REPO
    ROOT -.装配.-> PG
    ROOT -.装配.-> RD
    ROOT -.装配.-> CLI
```

**三条硬边界**：

1. 本模块**只实现协议，不定义协议** —— `Database` / `Transaction` 在 `src/repo/base.py`，
   限流后端协议在 `src/gateway`。本模块是**被注入**的一方。
2. 本模块**不被 `src/` 引用** —— 依赖方向是 `apps → src`，反向不存在。
3. 本模块**不做组合** —— 把谁接到谁身上是组合根的事。

---

## 5. 功能需求

编号 `FR-S-xx`。

### 5.1 Postgres

#### FR-S-01 — Postgres 装配

**要求**：

- 按 `postgres.dsn` 建异步引擎与连接池；池参数可配且有**默认上限**；
- 引擎是**进程内单例**（一个进程一个池），不得每个仓储新建；
- 暴露一个 `Database` 实现（事务边界），交给组合根注入 `src/repo`；
- **不回显 SQL**，除非显式打开 `postgres.echo`（回显会把参数连敏感值一起打进日志）。

**验收点**：同一进程内构造两次 `Database` → 拿到的是同一个池（`engine.pool is engine.pool`）。

#### FR-S-02 — 连接预算

**要求**：

- 本机实测 `max_connections = 100`（PG 17.11），且**多进程共享**这个上限；
- 默认池参数必须**小**（宁肯让需要的人显式调大），并在启动时对「`pool_size × 预期进程数` 过大」告警；
- 连接**必须**在关停时释放（`foundation/container.py`：构造了不释放 = 连接泄漏，长跑进程表现为「跑几小时就连不上」，且症状离原因很远）。

**验收点**：启动 4 个 worker，每个 pool_size=5 → 峰值连接 ≤ 40；全部关停后 `pg_stat_activity` 里本应用的连接数归零。

#### FR-S-03 — 启动期健康检查与明确报错

**要求**：

- 启动时连一次，确认：库存在、`vector` 扩展可用、迁移版本与代码期望一致；
- 三者任一不满足 → **明确报错并拒绝启动**，错误信息里带上「当前库名」；
- **特别地**：如果连上的是 `besa` 库（另一个项目的库）→ 必须明确拒绝，
  因为两个项目的 `alembic_version` 会互相覆盖。

**验收点**：把 DSN 指向 `besa` → 启动失败，错误信息里出现 `besa` 与原因。

### 5.2 Redis

#### FR-S-04 — Redis 装配

**要求**：

- 按 `redis.url` 建客户端（连接池），进程内单例；
- 启动时 ping 一次，确认可达与版本；
- 暴露健康状态供 `doctor` 查询。

**验收点**：Redis 未启动时，`doctor` 能报出「Redis 不可达」而不是抛栈。

#### FR-S-05 — 多进程限流后端（`FR-G-06` 的落地）

**要求**：

这是本模块**价值最高**的一条。

- 实现 `src/gateway` 的限流后端协议，使 `rate_limit.backend: redis` **可用**；
- 支持 **RPM / TPM / 并发** 三个维度跨进程生效；
- **原子性**：计数与判定必须是一个原子操作（否则多进程下「读-判断-写」之间会超发）；
- **TPM 必须支持预扣与回补**（与 `gateway.rate_limit` 的 `reconcile` 语义对齐）——
  注意 gateway 侧的 `reconcile` 目前**零调用点**，本后端实现时必须同时把它接上；
- Redis 不可用时的行为**必须显式配置**，且**不得静默降级**为单进程（见 `NFR-S-02`）。

**验收点**：两个进程同时各发起超过配额的请求 → 实际通过的总数**不超过**配额。

#### FR-S-06 — 分布式锁

**要求**：

- 提供带**过期时间**的互斥锁，供断点续跑使用（同一个 `run_id` 不得被两个进程同时续跑）；
- 持锁期间需要**续期**（长时间运行的任务不能用固定 TTL 硬扛）；
- 释放必须校验持有者（不得释放别人的锁）；
- 进程崩溃时锁**必须能自动过期**（不依赖优雅关闭）。

**验收点**：两个进程同时尝试获取同一 `run_id` 的锁 → 只有一个成功；持锁进程被 kill → 锁在 TTL 后自动可获取。

#### FR-S-07 — 缓存

**要求**：

- 只缓存**可丢失**的数据（查询结果、配置投影、模型元数据）；
- 每个键**必须**有过期时间（无 TTL 的缓存键 = 内存泄漏）；
- **权威数据不得只存在于 Redis** —— 丢了必须能从事务库重建；
- 缓存未命中必须**回源**，不得把「没有」当成「空值」缓存（那会让一次抖动变成持续的错误）。

**验收点**：清空 Redis 全部键 → 应用功能不受影响（只是变慢）。

#### FR-S-08 — 跨进程事件分发（可选）

**要求**：

- 提供 `src/event` 的跨进程传输（Redis 5.0 有 Streams，可用）；
- **可丢失**：事件分发的失败不得影响主流程（与 `FR-G-10` 的「事件发布不得阻塞调用链路」一致）；
- 本模块只提供**传输**，事件的语义与字段定义属于 `src/event`。

### 5.3 向量

#### FR-S-09 — pgvector 实现

**要求**：

- 实现 `src/repo` 定义的 `VectorStore` 协议（具体表模型与 SQL 在本模块，见架构文档）；
- 表模型必须注册到 **`foundation/db.Base`**，与 repo 的表**同库同事务**（这是 `FR-R-08` 的前提）；
- 按维度路由到 `embeddings_<dim>` 表；维度不匹配**必须报错**，不得截断或补齐；
- 检索返回**距离**，不只是 top-k；
- 索引用 HNSW。

**验收点**：写入 5 条向量后最近邻的第一条与手工计算一致；用错误维度检索 → 报错。

### 5.4 生命周期

#### FR-S-10 — 关停顺序

**要求**：

关停顺序是**有约束的**，不是随便排的：

```
1. 停止接受新请求
2. ★ 让 gateway drain 它的 UsageLedger（否则缓冲区里的用量直接消失）
3. 提交/回滚在途事务
4. 释放 Redis 客户端
5. 释放 Postgres 连接池
6. 释放 provider 的 HTTP 客户端（container 已管）
```

**第 2 步必须排在第 3 步之前**：要落库的数据得先进入事务。
而 gateway 的 `aclose()` **不 drain**（本次架构分析记录在案），所以这一步要在组合根里显式做。

**验收点**：跑一次会产生用量的调用 → 立即关停 → 用量记录**在库里**（不是只有内存里）。

#### FR-S-11 — CLI 的存储策略

**要求**：

- `apps/cli/storage/` 默认提供**内存实现**（不需要任何外部依赖，这是项目「无 DB 可跑」的前提之一）；
- 需要落库时，通过配置切到 Postgres（复用 `apps/server/storage/postgres/`）；
- CLI **不接 Redis**（单进程没有跨进程需求）。

---

## 6. 非功能需求

| 编号 | 需求 | 说明 |
|---|---|---|
| `NFR-S-01` | **只实现协议，不定义协议** | 协议在 `src/`，本模块是被注入的一方。改协议不该改本模块 |
| `NFR-S-02` | **不得静默降级** | Redis 不可用时，**要么明确拒绝启动，要么降级并大声告警**，绝不能不吭声地退化成单进程（那正是 `FR-G-06` 的报错在防的事） |
| `NFR-S-03` | **权威数据不放 Redis** | 判据：**能丢的才进 Redis**。我们的键全部丢失时功能仍正确，只影响性能（注意：验证这条只能清 `besa_agent:` 前缀，不能全库清 —— 见 `C-S-4`） |
| `NFR-S-04` | **连接预算受控** | 见 `FR-S-02`；Redis 侧同理（本机 Redis 是单实例，无 maxclients 配置，但不能无限开） |
| `NFR-S-05` | **可测试** | 装配层要能用假引擎/假 Redis 测；不得让单测依赖真实后端 |
| `NFR-S-06` | **不碰 `besa` 库** | 本机 `besa` 库属于另一个项目（`besa-iv-kb`）；见 `FR-S-03` |
| `NFR-S-07` | **启动快** | 健康检查要有超时且可跳过（CI 里不该因为探活慢而卡住） |

---

## 7. 配置需求

```yaml
postgres:
  dsn: ${BESA_POSTGRES_DSN:-postgresql+asyncpg://besa:besa@127.0.0.1:5432/besa_agent}
  pool:
    size: 5
    max_overflow: 5
    timeout_s: 30
  echo: false
  healthcheck:
    enabled: true
    timeout_s: 3
    # 期望的迁移版本；与代码不符时拒绝启动
    require_migration: true

redis:
  url: ${BESA_REDIS_URL:-redis://127.0.0.1:6379/0}
  healthcheck:
    enabled: true
    timeout_s: 2
  # ★ 不可用时的行为，必须显式选
  on_unavailable: fail_start     # fail_start | degrade_with_alert
  rate_limit:
    # ⚠ 前缀是 besa_agent: 而不是 besa: —— 本机 6379 上 `besa:` 已被兄弟项目
    # （besa-iv-kb 的 Celery 队列）占用，实测键：besa:queue:ingest、_kombu.binding.*
    key_prefix: "besa_agent:rl:"
    # 本地预取额度：减少 Redis 往返（NFR-G-03 的 1ms 预算主要给这里）
    local_prefetch: true
    prefetch_batch: 10
  lock:
    key_prefix: "besa_agent:lock:"
    ttl_s: 60
    renew_interval_s: 20
  cache:
    key_prefix: "besa_agent:cache:"
    default_ttl_s: 300
    max_keys: 100000               # 自我保护：Redis 实测 maxmemory=0，没有淘汰兜底

vector:
  space: default             # 向量空间名（见 docs/repo 架构文档 §3.4）
  index: hnsw                # hnsw | ivfflat
  hnsw:
    m: 16
    ef_construction: 64
```

**要求**：

- `C-S-1` `on_unavailable` **没有默认值**，必须显式配 —— 逼配置者做决定（`NFR-S-02`）；
- `C-S-2` 全部 Redis 键带统一前缀 `besa_agent:`，避免与同实例上的其它项目撞键。
  **实测**：本机 6379 上已有 `besa:queue:ingest` 与 `_kombu.binding.*`（一个 **Celery** 部署），
  所以 `besa:` 这个命名空间**不能再用**，也**不能假设这个 Redis 只属于我们**；
- `C-S-3` 池参数可配且有默认上限（默认值必须考虑 100 的总预算）；
- `C-S-4` **严禁 `FLUSHDB` / `FLUSHALL`**，生产环境禁止 `KEYS *` —— 会清掉同实例上别人的 Celery 队列。
  这条要同时写进代码注释与运维文档。

---

## 8. 验收标准（场景化）

| # | 场景 | 期望 |
|---|---|---|
| `CS-1` | 同一进程构造两次 Database | 同一个池 |
| `CS-2` | 4 worker 跑满 | 峰值连接 ≤ `pool_size × (1 + overflow) × 4`，关停后归零 |
| `CS-3` | DSN 指向 `besa` 库 | 拒绝启动，错误里带上库名与原因 |
| `CS-4` | Redis 未启动 + `on_unavailable: fail_start` | 拒绝启动并说明 |
| `CS-5` | Redis 未启动 + `degrade_with_alert` | 降级但打 WARNING，且 `doctor` 可见不可用 |
| `CS-6` | 两进程同时超配额请求 | 通过总数不超过配额 |
| `CS-7` | 两进程抢同一 `run_id` 的锁 | 只有一个成功 |
| `CS-8` | 持锁进程被 kill | 锁在 TTL 后自动可获取 |
| `CS-9` | 清掉**本项目的** Redis 键（`besa_agent:` 前缀，**不是**全库清） | 功能正确，只是变慢 |
| `CS-10` | 缓存未命中 | 回源取真值，不把「没有」缓存成空值 |
| `CS-11` | 错误维度检索向量 | 明确报错 |
| `CS-12` | 产生用量后立即关停 | 用量记录在库里 |
| `CS-13` | 全仓库检索 | `src/` 不 import `apps/` |
| `CS-14` | 全仓库存放的 Redis 键 | 全部带统一前缀 |

---

## 9. 决策点（需评审确认）

| 编号 | 决策点 | 备选 | 建议 |
|---|---|---|---|
| `DS-1` | Redis 的**角色边界** | (a) 只做限流+锁；(b) 再加缓存；(c) 再加上事件与队列 | **(b)** —— 缓存是明确收益（查询结果、模型元数据），而事件分发与队列会引入「消息丢了怎么办」这个新问题，一期不做（`NFR-S-03`：能丢的才进 Redis） |
| `DS-2` | Redis 不可用时的默认行为 | (a) 拒绝启动；(b) 降级+告警；(c) 熔断限流 | **(a) 为默认，可配 (b)** —— 与 gateway 对 `backend: redis` 的报错同源：**静默降级是这类失效最危险的形态**（本地全绿、生产超发 N 倍）。但要让运维显式选择 (b) |
| `DS-3` | 多进程限流的算法 | (a) Redis 计数 + 本地预取额度；(b) 纯 Redis 每请求往返；(c) 本地令牌桶 + 定期同步 | **(a)** —— `NFR-G-03` 要求 gateway 单跳 <1ms，而「纯 Redis 每个请求一次往返」会把这个预算全吃掉。(c) 的问题是同步周期内的超发不可控 |
| `DS-4` | 限流计数用 Lua 还是 `INCR` + `EXPIRE` | (a) Lua 脚本；(b) 多条命令 + `MULTI` | **(a)** —— 滑动窗口需要「读-判断-写」三步原子，`INCR`+`EXPIRE` 表达不了；Redis 5.0 支持 Lua |
| `DS-5` | 锁的续期方式 | (a) 看门狗协程；(b) 每次业务操作手动续；(c) 长 TTL 不续 | **(a)** —— (b) 会漏、(c) 崩溃后锁要等很久 |
| `DS-6` | pgvector 的实现位置 | (a) `src/repo`；(b) `apps/server/storage/postgres/` | **(b)** —— `vector(N)` / `<=>` / HNSW 都是 Postgres 特有的，属于「必须绑定具体后端」；`src/repo` 只定义协议 |
| `DS-7` | CLI 的默认存储 | (a) 纯内存；(b) SQLite；(c) 接本机 Postgres | **(a)** —— 保住「无 DB 可跑」这条项目级前提；需要时显式切 PG |
| `DS-8` | 是否复用同一个 Redis 实例 | (a) 复用（本机 6379）；(b) 要求独立实例 | **(a) + 统一键前缀** —— 本机 6379 上已有其它键，不能假设独占；但**必须**在文档里写明这一条 |

---

## 10. 与相邻模块的契约

| 相邻模块 | 契约 |
|---|---|
| `src/repo` | 本模块**实现**它定义的 `Database` / `Transaction` / `VectorStore` 协议 |
| `src/gateway` | 本模块**实现**它的限流后端协议；不改它的决策逻辑 |
| `src/event` | 本模块只提供跨进程传输；事件的语义归 event |
| `foundation/db.py` | 本模块**用**它的 Base 与命名约定建引擎，不改它 |
| `foundation/container.py` | 连接由 container 持有与释放；本模块提供**怎么建** |
| 组合根 `apps/*/runtime/` | 唯一把本模块与 `src/` 接起来的地方 |

---

## 11. 下一步

本文定稿后进入《`apps/server/storage` 架构概要设计》，届时需要产出：

- 引擎与池的构造流程、`Database` 实现的类图；
- 多进程限流后端的算法（滑动窗口 + 本地预取）与它在 `reconcile` 上的接线；
- 分布式锁的续期与持有者校验的具体设计；
- pgvector 的表模型、索引与维度路由（与 `docs/repo` 的向量设计对齐）；
- 关停顺序的实现位置与 `drain` 的接线；
- 连接预算的算式（对着 `max_connections = 100`）；
- Redis 不可用时的两条路径（拒绝启动 / 降级告警）的具体表现。
