# BesaAgent

面向**软件测试全流程**的多智能体平台：需求 → 测试设计 → 用例 → 环境 → 执行 → 分析 → 缺陷。

## 快速开始

不需要任何密钥、数据库或网络 —— 默认配置全部指向 mock：

```bash
# 看装配结果：每个模型可用吗、每条逻辑名走哪条链
PYTHONPATH=src python -m apps.cli.main doctor

# 发一条消息
PYTHONPATH=src python -m apps.cli.main --env test runtime "帮我为登录接口设计测试用例"

# 跑测试与依赖方向契约
PYTHONPATH=src python -m pytest tests -q
PYTHONPATH=src lint-imports
```

接真实厂商：把密钥写进仓库根 `.env`（已 gitignore），然后 `--env dev`。

```bash
echo 'DEEPSEEK_API_KEY=sk-xxxxxxxx' > .env
PYTHONPATH=src python -m apps.cli.main --env dev runtime "..." --stream
```

## 代码结构

```
apps/                     可独立启动的进程（进程边界）
  cli/                    main.py + commands/ + presentation/ + runtime/
  mcp/                    MCP 服务（待实现）
  server/                 REST 服务（待实现）

src/
  foundation/             共享底座：settings errors types ids clock db database
                          provider logging factory container
                          ⚠ 不依赖任何同仓库模块（唯一豁免见下）

  composition/            组合根：bootstrap.build_runtime()
                          ⚠ 唯一允许 import 全部同仓库模块的地方，且无人 import 它

  provider/               厂商适配层：把各家 API 差异收敛在边界内
    base types errors       契约层（不依赖任何厂商子包）
    openai/                 OpenAI 兼容形状的**基准实现**
    dashscope/ vllm/        继承基准，只覆写差异
    mock/                   确定性假实现（CI 的默认路径）

  gateway/                模型网关：选谁、挂了怎么办、花多少、还活着吗
    gateway registry router retry fallback
    rate_limit health usage cost errors types

  agent/                  七个测试阶段的智能体（待实现）
  multiagent/ context/ chat/ memory/ repo/ skill/ tool/ event/   （部分待实现）

configs/                  配置组 ↔ 能力 ↔ 实现，见 configs/README.md
tests/unit/               镜像 src 的结构
```

依赖方向由 `pyproject.toml` 里的 **import-linter 契约**机械强制，不是靠自觉：

```
foundation  ←  provider  ←  gateway  ←  agent / multiagent / ...
composition →（可以依赖任何模块，但没有任何模块依赖它）
```

四条契约当前全部 KEPT，跑法 `PYTHONPATH=src lint-imports`。

## 文档

| 文档 | 层级 |
|---|---|
| [`docs/需求说明书-provider.md`](docs/provider/需求说明书-provider.md) | 需求 |
| [`docs/需求说明书-gateway.md`](docs/gateway/需求说明书-gateway.md) | 需求 |
| [`docs/架构概要设计-provider.md`](docs/provider/架构概要设计-provider.md) | 架构 |
| [`docs/架构概要设计-gateway.md`](docs/gateway/架构概要设计-gateway.md) | 架构 |
| [`configs/README.md`](configs/README.md) | 配置 |

## 几个刻意的设计选择

**`provider` 与 `gateway` 的分工**：provider 只回答「这个请求在这个厂商的 API 上怎么写」，
gateway 回答「用哪个、挂了怎么办、花了多少」。

**重试不叠加**：provider 只重试**网络层**错误（连接失败 / 读超时），
HTTP 状态码错误一律上抛给 gateway。理由是厂商故障需要**跨模型**决策，
且两层的重试会相乘 —— 那是重试风暴的主要来源。全局上限由
`CallBudget.try_acquire()` 这一个计数点封顶。

**`None` 与 `0` 的区别**：token 用量与成本都用 `None` 表示「未知」。
`0` 是「免费」，`None` 是「不知道」，混同会让报表静默失真。

**能力是模型的事实，不是厂商的事实**：`vision` 与 `embedding` 不在任何厂商的默认能力集里 ——
`qwen-vl` 才有视觉，`text-embedding-v3` 才做向量化，而它们共用一个适配器。

**不可用 ≠ 降级**：缺密钥的模型在选链阶段就被过滤（启动时警告，`doctor` 里可见），
`degraded=True` 只表示**运行时**故障导致的临时降级。混同会让
「这次是不是出了故障」这个问题永远无法回答。
