"""三个抽象契约：传输（``Client``）、能力（``ChatModel`` / ``EmbeddingModel``）、厂商（``Provider``）。

**本模块不 import 任何厂商子包**（需求说明书-provider NFR-P-06）。

**为什么 ``chat`` 是抽象方法，而不是「模板方法 + 抽象钩子」**
（``besa-iv-kb`` 实测踩过的坑）：测试替身只需要实现 ``chat`` 本身。
若把 ``chat`` 做成模板方法、把 ``_build_payload`` 做成抽象钩子，
假实现就被迫实现一个它**根本用不到**的 HTTP 载荷构造器 ——
后果是**假实现无法实例化**，而 mock 恰恰是 CI 能在无密钥环境跑通的前提（FR-P-15）。

所以本模块的约定是：**公开契约抽象，公共管线以普通辅助方法提供，由子类按需调用。**
各厂商的模板方法在各 ``llm.py`` 里自行组织，不在这里强加。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, ClassVar

from foundation.clock import Clock, SystemClock
from provider.errors import AuthError, CapabilityNotSupportedError
from provider.types import (
    VALID_ROLES,
    Capability,
    ChatRequest,
    ChatResponse,
    EmbeddingResult,
    Message,
    ModelConfig,
    has_any_image,
    has_image,
    message_text,
)

__all__ = [
    "ChatModel",
    "Client",
    "EmbeddingModel",
    "ModelBase",
    "Provider",
    "guard_request",
    "validate_messages",
]


# --------------------------------------------------------------------------- #
# 本地校验（FR-P-01 / FR-P-08）
# --------------------------------------------------------------------------- #


def validate_messages(messages: Sequence[Message], *, model: str = "") -> None:
    """在本地拦掉空列表 / 非法 role / 空内容。

    **为什么要本地拦**：厂商的 400 报文通常是「第 N 项参数非法」，
    既不说是哪条消息，也不说是哪个字段 —— 而在 agent 场景里，
    消息是**动态拼出来的**，出问题时调用方自己也不知道拼成了什么。

    Raises:
        ValueError: 校验失败。这是**编程错误**，不是运行时状况，所以不进错误树。
    """
    if not messages:
        raise ValueError(f"messages 不能为空（model={model or '未指定'}）")

    for index, message in enumerate(messages):
        if not isinstance(message, Message):
            raise ValueError(f"第 {index} 条不是 Message：{type(message).__name__}")
        if message.role not in VALID_ROLES:
            raise ValueError(
                f"第 {index} 条 role={message.role!r} 非法，必须是 {sorted(VALID_ROLES)}"
            )
        # 工具结果消息允许内容为空（工具可能返回空），其余角色不允许 ——
        # 空内容发出去，多数厂商会返回 400，而原因看起来像「请求格式错」。
        if message.role == "tool":
            continue
        if message.tool_calls:
            continue          # assistant 消息可以只有工具调用、没有正文
        # 只有图片、没有文字的消息是**合法**的（多模态场景的常见形态），
        # 只看文本会把它误判成空消息。
        if not message_text(message).strip() and not has_image(message):
            raise ValueError(f"第 {index} 条（role={message.role}）内容为空")


def guard_request(
    req: ChatRequest,
    capabilities: frozenset[Capability],
    *,
    model: str = "",
    provider: str = "",
) -> None:
    """照 ``capabilities`` 拦截请求，或对可容忍的差异降级。

    **拦截与降级的分界是刻意的**（架构概要设计-provider §4.5）：

    ==========================  ========  ==============================================
    请求                        行为      理由
    ==========================  ========  ==============================================
    含图片但无 ``vision``        拦截      透传给上游只会得到 400；而模型「看不见图」时
                                          若静默丢图，产出的是**看似正常但错误**的答案
    带工具但无 ``tools``         拦截      同上，且静默丢工具会让 agent 陷入「模型不调工具」
    要 ``json`` 但无 ``json``    **降级**  结果**仍然可用**（只是约束弱了），
                                          由 ``ChatResponse.structured_native=False`` 标记
    ``stream=True`` 但无 ``stream``  拦截   无法降级：调用方要的是流
    ==========================  ========  ==============================================

    Raises:
        CapabilityNotSupportedError: 触发拦截条件时。
    """
    missing: list[str] = []

    if req.stream and Capability.STREAM not in capabilities:
        missing.append("stream")
    if req.tools and Capability.TOOLS not in capabilities:
        missing.append("tools")
    if has_any_image(req.messages) and Capability.VISION not in capabilities:
        missing.append("vision")

    if missing:
        raise CapabilityNotSupportedError(
            f"模型 {model!r} 不具备能力：{', '.join(missing)}；"
            f"已声明能力：{', '.join(sorted(c.value for c in capabilities)) or '（无）'}",
            provider=provider,
            model=model,
            trace_id=req.trace_id,
        )

    # json 走降级而非拦截 —— 见上表。这里只做「能不能走原生」的判断，
    # 真正降级发生在 payload 构造处（各 llm.py 读 supports_json_native）。
    if Capability.CHAT not in capabilities:
        raise CapabilityNotSupportedError(
            f"模型 {model!r} 不具备对话能力",
            provider=provider,
            model=model,
            trace_id=req.trace_id,
        )


# --------------------------------------------------------------------------- #
# 传输契约
# --------------------------------------------------------------------------- #


class Client(ABC):
    """HTTP 传输契约。**唯一允许 import ``httpx`` 的地方**（各厂商的 ``client.py``）。

    **``stream_sse`` 刻意声明为普通 ``def``**（架构概要设计-provider §4.1）：
    若声明成 ``async def`` + ``yield``，调用时**不会执行任何代码** ——
    连 URL 拼接、头部构造、能力校验都不会跑，要等第一次 ``__anext__``。
    那会让「配置指错了地址」这个错误发生在离调用点很远的地方。
    """

    #: 该客户端指向的厂商名，仅用于错误信息
    provider_name: str = ""

    @abstractmethod
    async def post_json(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        timeout_s: float | None = None,
        trace_id: str = "",
    ) -> dict[str, Any]:
        """POST 一个 JSON 请求体，返回解析后的响应体。

        Raises:
            ProviderError: 全部失败路径都已归一化（见 ``errors.map_http_status``）。
            asyncio.CancelledError: **原样传播**，不包装。
        """
        ...

    @abstractmethod
    def stream_sse(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        timeout_s: float | None = None,
        trace_id: str = "",
    ) -> AsyncIterator[str]:
        """POST 并逐行产出 **SSE 行**（而非正文增量）。

        拆行是传输层的事，从行里取 ``delta.content`` 是 ``llm.py`` 的事。
        这条边界让非 SSE 形状的流式协议（如 Ollama 的 ``/api/chat``）
        只需要覆写 ``llm.py`` 的解析，不必动传输层。
        """
        ...

    @abstractmethod
    async def aclose(self) -> None:
        """释放连接。**必须幂等** —— ``container`` 关停时可能重复调用。"""
        ...


# --------------------------------------------------------------------------- #
# 模型契约
# --------------------------------------------------------------------------- #


class ModelBase(ABC):
    """``ChatModel`` 与 ``EmbeddingModel`` 的公共部分：配置、能力、生命周期。"""

    def __init__(self, cfg: ModelConfig, client: Client) -> None:
        self._cfg = cfg
        self._client = client
        # 能力在这里**定形**，而不是每次调用时算 —— 调用路径上不应有分支。
        self._capabilities = cfg.capabilities_or(self.default_capabilities())

    @classmethod
    def default_capabilities(cls) -> frozenset[Capability]:
        """该适配器的默认能力集。子类覆盖。"""
        return frozenset({Capability.CHAT})

    @property
    def config(self) -> ModelConfig:
        return self._cfg

    @property
    def model_name(self) -> str:
        return self._cfg.model

    @property
    def provider_name(self) -> str:
        return self._cfg.provider

    def capabilities(self) -> frozenset[Capability]:
        return self._capabilities

    def supports(self, capability: Capability) -> bool:
        return capability in self._capabilities

    async def aclose(self) -> None:
        await self._client.aclose()


class ChatModel(ModelBase):
    """对话补全契约。"""

    @abstractmethod
    async def chat(self, req: ChatRequest) -> ChatResponse:
        """生成一次完整回复。

        实现方**应当**先调 :func:`validate_messages` 与 :func:`guard_request`，
        再发请求 —— 公共管线以辅助方法形式提供，由子类按需调用（见模块 docstring）。
        """
        ...

    @abstractmethod
    def stream_chat(self, req: ChatRequest) -> AsyncIterator[str]:
        """逐段产出**增量正文**。

        **声明为 ``def`` 而非 ``async def``**：见 :class:`Client.stream_sse` 的说明 ——
        ``async def`` + ``yield`` 会推迟到首次迭代才执行校验，让「请求了不支持流式的模型」
        这个错误发生在离调用点很远的地方（FR-P-08 要求本地拦截）。
        """
        ...


class EmbeddingModel(ModelBase):
    """向量化契约。"""

    @classmethod
    def default_capabilities(cls) -> frozenset[Capability]:
        return frozenset({Capability.EMBEDDING})

    @classmethod
    def default_max_batch(cls) -> int:
        """单次请求的条数上限。厂商间差异大（DashScope 比 OpenAI 严），故做成本类知识。"""
        return 32

    def resolve_max_batch(self, override: int | None = None) -> int:
        """批大小的优先级：**调用参数 > 配置 > 厂商默认**。"""
        return max(1, int(override or self.config.max_batch or self.default_max_batch()))

    @abstractmethod
    async def embed(
        self,
        texts: Sequence[str],
        *,
        batch_size: int | None = None,
        trace_id: str = "",
    ) -> EmbeddingResult:
        """批量向量化。

        **部分失败的语义**（FR-P-07）：整批失败并标明**失败下标**，
        不得返回长度不符的结果。返回短一截的向量列表，
        会让调用方把第 7 条的结果当成第 8 条的 —— 且这个错位**不会报错**。
        """
        ...


# --------------------------------------------------------------------------- #
# 厂商契约
# --------------------------------------------------------------------------- #


class Provider(ABC):
    """厂商描述：静态事实 + 装配入口。**不碰 HTTP**。

    纯声明意味着本类的子类可以在**无网络环境**下被单测直接断言
    （架构概要设计-provider §1.2 的契约层性质）。
    """

    #: 厂商标识，与配置里 ``provider:`` 的值对应
    NAME: ClassVar[str] = ""
    #: 该厂商的默认端点
    DEFAULT_BASE_URL: ClassVar[str] = ""
    #: 该厂商密钥的约定环境变量名
    API_KEY_ENV: ClassVar[str] = ""
    #: 该厂商是否**必须**有密钥。云端 True → 构造即报错；
    #: 本地端点（自建 vLLM）保持 False，无密钥照常工作（FR-P-12）。
    REQUIRES_API_KEY: ClassVar[bool] = False
    #: 未显式声明能力时的默认集
    DEFAULT_CAPABILITIES: ClassVar[frozenset[Capability]] = frozenset({Capability.CHAT})

    def __init__(
        self,
        cfg: ModelConfig,
        *,
        transport: Any | None = None,
        env: Mapping[str, str] | None = None,
        clock: Clock | None = None,
    ) -> None:
        """
        Args:
            cfg: 已经过投影与凭据解析的模型配置。
            transport: ``httpx.AsyncBaseTransport``，**仅供测试注入**（FR-P-13）。
            env: 环境变量来源，仅供测试注入。
            clock: 可注入时钟，测试用 ``FakeClock`` 秒过传输层退避。
        """
        self._cfg = cfg
        self._transport = transport
        self._env = env
        self._clock: Clock = clock or SystemClock()
        self._client: Client | None = None
        self._validate_credentials()

    # ---------------------------------------------------------------- 校验
    @property
    def api_key_env(self) -> str:
        """该模型实际使用的凭据变量名。

        **配置里显式给了 ``api_key_env`` 就以它为准，不再回退到适配器的类属性。**
        这一条很容易写反，而写反的后果是**错误信息指错方向**：
        ``chat-deepseek`` 走的是 openai 适配器（OpenAI 兼容协议），
        凭据来自 ``providers.deepseek.api_key_env = DEEPSEEK_API_KEY``。
        若类属性 ``OPENAI_API_KEY`` 还能兜底并出现在报错里，
        用户会去设一个完全无关的变量，然后疑惑「设了怎么还是不行」。
        """
        return self._cfg.api_key_env or self.API_KEY_ENV

    def _resolve_api_key(self) -> str:
        """兜底解析：前三级（model 级 api_key / api_key_env / 厂商段）已在 ``ModelConfig`` 里算完。"""
        if self._cfg.api_key:
            return self._cfg.api_key

        var = self.api_key_env
        if not var:
            return ""

        import os

        source = os.environ if self._env is None else self._env
        return str(source.get(var) or "").strip()

    def _validate_credentials(self) -> None:
        """云厂商缺失密钥 → **构造期**报错（FR-P-12）。

        等到第一次调用才报 401，会让「配置写错了」伪装成「网络/上游问题」——
        这两类的排查方向完全不同，而伪装是单向的：网络问题从不像配置问题。
        """
        key = self._resolve_api_key()
        required = self._cfg.require_api_key
        if required is None:
            required = self.REQUIRES_API_KEY
        if key or not required:
            return
        # 提示里给的必须是**这个模型实际读的那个变量名**，不是适配器的类属性 ——
        # 见 :attr:`api_key_env`。
        hint = f"，或设置环境变量 {self.api_key_env}" if self.api_key_env else ""
        raise AuthError(
            f"{type(self).__name__} 缺少 API key：请在 model 上写 api_key / api_key_env{hint}",
            provider=self.NAME,
            model=self._cfg.model,
        )

    # ---------------------------------------------------------------- 能力
    def capabilities(self) -> frozenset[Capability]:
        """配置覆盖 > 厂商默认（FR-P-08）。

        **vLLM 这类自建服务的能力必须在配置里声明**：它取决于部署时加载了什么模型、
        开了哪些启动参数，编译期不可能知道（架构概要设计-provider §3.3）。
        """
        return self._cfg.capabilities_or(self.DEFAULT_CAPABILITIES)

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities()

    # ---------------------------------------------------------------- 装配
    @property
    def config(self) -> ModelConfig:
        return self._cfg

    def client(self) -> Client:
        """懒建并**缓存**传输客户端 —— 连接复用是 FR-P-13 的硬要求。"""
        if self._client is None:
            self._client = self._make_client()
        return self._client

    @abstractmethod
    def _make_client(self) -> Client:
        """构造该厂商的传输客户端。子类实现；**只在 ``client()`` 里被调用一次**。"""
        ...

    @abstractmethod
    def chat_model(self) -> ChatModel:
        """构造对话模型实例。"""
        ...

    def embedding_model(self) -> EmbeddingModel:
        """构造向量化模型实例。

        **默认抛异常而不是返回 ``None``**：``vllm/`` 没有 ``embedding.py``，
        所以「不支持向量化」是**正常路径** —— 但它应当在更上层就被能力过滤拦掉。
        走到这里说明能力过滤失效了，那是**编程错误**，必须立刻暴露。
        返回 ``None`` 会让调用方写出 ``if m is None:`` 的检查，
        而这个检查在错误的地方，且会悄悄掩盖过滤失效。
        """
        raise CapabilityNotSupportedError(
            f"{type(self).__name__} 不支持向量化",
            provider=self.NAME,
            model=self._cfg.model,
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---------------------------------------------------------------- 便捷
    @classmethod
    def from_config(
        cls,
        model_cfg: Mapping[str, Any],
        *,
        providers: Mapping[str, Any] | None = None,
        transport: Any | None = None,
        env: Mapping[str, str] | None = None,
        clock: Clock | None = None,
        **options: Any,
    ) -> Provider:
        """从配置构造。

        ``**options`` 原样转交子类构造器 —— 让 ``MockProvider`` 的 ``script``
        这类**厂商私有**参数不需要污染基类签名（``build_provider`` 靠它做统一入口）。
        """
        cfg = ModelConfig.from_config(model_cfg, providers=providers, env=env)
        if not cfg.base_url:
            cfg = _with_default_base_url(cfg, cls.DEFAULT_BASE_URL)
        return cls(cfg, transport=transport, env=env, clock=clock, **options)

    def __repr__(self) -> str:
        # 不输出 cfg：它含 api_key（NFR-P-04）
        return f"{type(self).__name__}(model={self._cfg.model!r}, provider={self.NAME!r})"


def _with_default_base_url(cfg: ModelConfig, default: str) -> ModelConfig:
    """填上厂商默认端点。缺 base_url 且厂商也没默认值 → 报错并指明怎么配。

    报错信息里必须说清「两种补法」：写 model 的 ``base_url``，或声明 ``provider:``
    由 providers 段提供。只说「缺少 base_url」会让不熟悉投影规则的人卡住。
    """
    from dataclasses import replace

    if not default:
        raise ValueError(
            f"缺少 base_url：请在 model 上写 base_url，"
            f"或声明 `provider: <厂商>` 由 providers 段提供"
        )
    return replace(cfg, base_url=default.rstrip("/"))
