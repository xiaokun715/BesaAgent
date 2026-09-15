# configs：配置组 ↔ 能力 ↔ 实现

这一层是**可插拔装配的入口**。改一个 `type` 或一个 `candidates` 列表，
就是在换实现或换模型 —— **业务代码一行都不用改**。

这是《重构文件结构设计》§2.2 那条判据（「换一个实现要不要改业务代码」）
在配置侧的样子，也是 `需求说明书-provider` FR-P-08 与
`需求说明书-gateway` FR-G-02 要求的落地方式。

## 文件

| 文件 | 用途 |
|---|---|
| `base.yaml` | 全环境共享的默认值。**默认全部指向 mock** —— 这是 CI 能在无网络、无密钥、无数据库环境跑通的前提 |
| `dev.yaml` | 本地联调：把候选链指向真实厂商 |
| `test.yaml` | 单测与 CI：**再钉一遍 mock** |

加载顺序：`base.yaml` → `<env>.yaml` **深度合并**（`foundation/settings.py`）。
`${VAR:-default}` 在加载期做环境变量插值。

`test.yaml` 存在的意义不是「和 base 一样」，而是**对 base 的改动免疫** ——
base 是给人改的（有人会把它切到真实厂商做本地调试），
而 CI 必须稳定。否则某次「顺手改一下 base」会让 CI 开始真的去调外部 API：
慢、不稳定、还会在别人的账单上体现。

## 三类配置段

### 1. `providers:` —— 厂商的端点与凭据

```yaml
providers:
  openai:
    base_url: https://api.openai.com/v1
    api_key_env: OPENAI_API_KEY      # ← 只写变量名，不写值
```

**密钥只以变量名的形式出现在配置文件里。** 值放仓库根 `.env`（已 gitignore）
或进程环境。这条约束是**结构性**的：配置里写不出密钥本体，
所以它不会进版本库、不会被贴进 issue、不会出现在日志里。

### 2. `models:` —— 物理模型

`provider` 与 `vendor` 的区别很关键：

| 字段 | 选什么 | 取值 |
|---|---|---|
| `provider` | **适配器**（协议形状） | `openai` / `dashscope` / `vllm` / `mock` |
| `vendor` | **端点与凭据**（`providers:` 段的键） | 缺省时取 `provider` |

普通厂商只写 `provider` 就够了。需要分开的是那种
**「走 OpenAI 兼容协议，但端点和密钥是自己的」**厂商 ——
DeepSeek / SiliconFlow / Moonshot 都是：

```yaml
  chat-deepseek:
    provider: openai        # 适配器：OpenAI 兼容形状
    vendor: deepseek        # 端点与凭据：取自 providers.deepseek
    model: deepseek-chat
```

没有 `vendor` 的话就只能二选一：要么共享真 OpenAI 的 `base_url`（错），
要么在每个 model 上重复写 `base_url` + `api_key_env`（啰嗦，且忘写一个就会静默连错地方）。

#### `capabilities:` 的两种写法语义不同

```yaml
capabilities: [chat, stream, tools]     # 列表 = 完整声明，**替换**厂商默认
capabilities: {vision: true}            # 映射 = 增量，**叠加**在厂商默认之上
```

这个区别不是风格选择：vLLM 的默认能力只有 `chat` + `stream`，
想**再加**一个 `tools` 时写 `{tools: true}` —— 若按「替换」解释，
用户会意外丢掉 `chat`，得到一个连对话都不支持、且报错完全指不到配置的模型。

**向量化模型必须显式收窄**：

```yaml
  emb-dashscope:
    provider: dashscope
    model: text-embedding-v3
    capabilities: [embedding]        # ← 不收窄会继承 dashscope 的完整能力集（含 chat）
```

不收窄的后果是**对话请求可能被路由到 `text-embedding-v3`**，
然后在上游得到一个指不到配置的 400。

### 3. `gateway:` —— 逻辑名、重试、降级、限流、熔断、计价

```yaml
gateway:
  aliases:
    chat.default:
      candidates: [chat-deepseek, chat-qwen, chat-mock]   # 顺序即优先级
      strategy: [capability, priority]
```

业务只认 `chat.default`，物理模型完全由 `candidates` 决定 —— **换模型 = 改这里**。

其余各段的作用与取值见 `base.yaml` 里的行内注释。三条最值得记住的：

- **`retry.total_max_attempts` 是跨候选累计的上游调用上限。**
  「重试 × 候选」本会相乘（3×3=9），靠这一个数字封顶。
- **`rate_limit.backend` 目前只支持 `local`（进程内）。** 配 `redis` 会**明确报错**
  而不是静默退化成单进程 —— 后者在多进程部署下会让实际配额超发 N 倍，
  而本地测试完全正常，是最难排查的一类问题。
- **`cost.prices` 里没配价格的模型，成本标记为「未知」而不是 0。**
  0 是「免费」，`None` 是「不知道」，混同会让成本报表静默失真。

## 快速上手

```bash
# 看装配结果：每个模型可用吗、每个逻辑名走哪条链
PYTHONPATH=src python -m apps.cli.main doctor

# 发一条消息（默认走 mock，无需任何密钥）
PYTHONPATH=src python -m apps.cli.main --env test chat "帮我为登录接口设计测试用例"

# 用真实厂商：把密钥写进仓库根 .env
cat > .env <<'EOF'
DEEPSEEK_API_KEY=sk-xxxxxxxx
EOF
PYTHONPATH=src python -m apps.cli.main --env dev chat "..." --stream
```

**没有密钥也能跑通全链路** —— 缺密钥只让对应模型标记为「不可用」
（`doctor` 里显示 ✗ 并给出原因），链路自动落到 mock。
这是 `NFR-G-05` 的要求，也是 `Registry` 用**软失败 + 保留原因**
而不是直接丢掉不可用模型的原因：丢掉就没得看了。
