"""共享底座：被几乎所有模块依赖、且**不含业务规则**的东西。

**判据**（沿用 besa-iv-kb《重构文件结构设计.md》§2.4）：**它能不能在三方库里找到同义物？**

    能  → 放这里：settings errors types ids db database provider logging factory container
    不能 → 不放这里：
        src/gateway 的 capability（模型能力语义）、alias（逻辑名规则）、
        retry_policy（重试策略）、cost_sheet（价格表）
        src/provider 的 errors（「是否可重试」是厂商契约的一部分）
        src/agent 的七阶段定义、src/event 的事件类型

一句话：**foundation 里放「换个项目也成立」的东西，带业务判断的一律下沉到各自模块。**

**依赖方向**：本包**不依赖任何同仓库模块**（只有标准库与三方库）。
因此 `provider` / `gateway` / `repo` / `event` / `agent` / `multiagent` … 中的任何一层
都可以依赖它，而不会制造环。

反向一律禁止：**一旦 foundation import 了任何同仓库模块，它就不再是底座。**
唯一豁免是 `logging → observability.tracing`（日志格式串要盖 trace_id），
且必须是函数内 + try/except 的**延迟 import** —— 否则会在 import 期形成环。

**为什么必须独立成层**：这些文件若散在各自模块里，会出现三份平行的配置读取、
四份 UUID 收敛、两套错误基类。它们在「谁都不依赖、谁都能依赖」这个位置上才成立 ——
放错层就会被迫在 import-linter 契约里开豁免，而豁免一旦开始就收不回来。

**从 besa-iv-kb 继承的经验**：该仓库的 foundation 最初就散在 `core/` 下，
结果是 import-linter 契约不得不把整个 `core` 从 libs 的 forbidden 列表里排除。
本仓库在建结构时就把它独立出来，避免重演。

**本包不提供**：组合根（把各模块装配到一起的代码）。那属于 `apps/*/runtime/`，
是唯一允许 import 全部同仓库模块的地方 —— 放在 foundation 会让底座反向依赖业务。
"""
