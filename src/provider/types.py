"""厂商无关的数据载体。

**这一层存在的意义**：上层（``src/gateway`` 及以上）只认这里的类型，
永远看不到 ``choices[0].delta.content`` 这种厂商形状。厂商字段名到本文件的映射表
写在《架构概要设计-provider》§3.4，那张表就是各 ``llm.py`` 的全部规格。

**本模块不 import 任何厂商子包**（需求说明书-provider NFR-P-06）——
它是契约层。反向依赖一旦出现，测试替身就会被逼着实现用不到的钩子。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Union

from foundation.provider import project_model_config, resolve_api_key

__all__ = [
    "VALID_ROLES",
    "Capability",
    "ChatRequest",
    "ChatResponse",
    "ContentPart",
    "EmbeddingResult",
    "ImagePart",
    "Message",
    "ModelConfig",
    "Role",
    "TextPart",
    "ToolCall",
    "ToolSpec",
    "Usage",
    "has_any_image",
    "has_image",
    "message_images",
    "message_text",
]

# --------------------------------------------------------------------------- #
# 消息
# --------------------------------------------------------------------------- #

Role = Literal["system", "user", "assistant", "tool"]

#: 非法 role 在**本地**拦截（FR-P-01），不等厂商返回 400 ——
#: 后者的错误信息通常是「第几项参数非法」，定位成本高得多。
VALID_ROLES: frozenset[str] = frozenset({"system", "user", "assistant", "tool"})

#: 结构化输出的取值。``json_schema`` 必须同时给出 schema，否则报错（FR-P-04）。
ResponseFormat = Literal["text", "json", "json_schema"]
VALID_RESPONSE_FORMATS: frozenset[str] = frozenset({"text", "json", "json_schema"})


@dataclass(frozen=True)
class TextPart:
    """消息中的文本片段。"""

    text: str


@dataclass(frozen=True)
class ImagePart:
    """消息中的图片片段（FR-P-06）。

    两种传法**互斥**：``url`` 指向可公开访问的图片，或 ``data`` 直接给 base64。
    同时给两者时以 ``data`` 为准（自包含，不依赖外部可达性）。
    """

    url: str | None = None
    data: str | None = None
    media_type: str = "image/png"
    detail: str | None = None

    def __post_init__(self) -> None:
        if not self.url and not self.data:
            raise ValueError("ImagePart 必须给出 url 或 data 之一")

    @property
    def data_uri(self) -> str:
        """base64 形式的 data URI —— 厂商要求的通用形状。"""
        return f"data:{self.media_type};base64,{self.data}"


ContentPart = Union[TextPart, ImagePart]


@dataclass(frozen=True)
class ToolCall:
    """模型请求调用一个工具（FR-P-03）。

    **同时保留 ``arguments_raw`` 与 ``arguments`` 是刻意的**：
    厂商返回的是**字符串**形式的 JSON，且**可能是非法 JSON**（模型幻觉）。
    只留解析结果 → 出错时原始串丢了，无法排障；
    只留原始串 → 每个消费者都要自己 ``json.loads`` 一遍，且解析口径会逐渐不一致。
    """

    id: str
    name: str
    arguments_raw: str = ""
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolSpec:
    """传给厂商的工具定义。``parameters`` 是 JSON Schema。"""

    name: str
    description: str = ""
    parameters: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Message:
    """一条消息。``content`` 是 ``str``（纯文本）或片段序列（多模态）。"""

    role: Role
    content: Union[str, tuple[ContentPart, ...]] = ""
    name: str | None = None
    #: 回填工具执行结果时，指向被响应的那个 ToolCall（FR-P-03 多轮往返）
    tool_call_id: str | None = None
    #: assistant 消息中模型发起的工具调用
    tool_calls: tuple[ToolCall, ...] = ()

    @classmethod
    def text(cls, role: Role, text: str) -> Message:
        return cls(role=role, content=text)

    @classmethod
    def tool_result(cls, tool_call_id: str, result: str) -> Message:
        return cls(role="tool", content=result, tool_call_id=tool_call_id)


def message_text(message: Message) -> str:
    """取消息中的纯文本部分（多模态消息则拼接所有文本片段）。"""
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(part.text for part in content if isinstance(part, TextPart))


def message_images(message: Message) -> tuple[ImagePart, ...]:
    """取消息中的图片片段；无则空元组。"""
    content = message.content
    if isinstance(content, str):
        return ()
    return tuple(part for part in content if isinstance(part, ImagePart))


def has_image(message: Message) -> bool:
    return bool(message_images(message))


def has_any_image(messages: Sequence[Message]) -> bool:
    """整段对话里是否含图片 —— ``vision`` 能力拦截用（FR-P-06）。"""
    return any(has_image(m) for m in messages)


# --------------------------------------------------------------------------- #
# 能力
# --------------------------------------------------------------------------- #


class Capability(str, Enum):
    """模型能力。``src/gateway`` 的路由与本地拦截都以它为依据（FR-P-08）。"""

    CHAT = "runtime"
    STREAM = "stream"
    TOOLS = "tools"
    JSON = "json"
    VISION = "vision"
    EMBEDDING = "embedding"


def _parse_capabilities(
    value: Any,
) -> tuple[frozenset[Capability] | None, Mapping[Capability, bool] | None]:
    """把配置里的能力声明解析成「完整集合」或「增量覆盖」。

    **两种写法语义不同，这不是风格选择而是硬约束**：:

        capabilities: [runtime, stream, tools]         # 列表 = 完整声明，**替换**厂商默认
        capabilities: {vision: true}                # 映射 = 增量，**叠加**在厂商默认之上

    差异的来源很实际：``vllm`` 的默认能力只有 ``runtime`` + ``stream``，
    用户想**再加**一个 ``tools`` 时写 ``{tools: true}`` ——
    若按「替换」解释，他就会意外丢掉 ``runtime``，得到一个连对话都不支持、
    且报错信息完全指不到配置的模型。

    Returns:
        ``(完整集合, 增量覆盖)``，两者至多一个非 ``None``。
    """
    if value is None:
        return None, None

    if isinstance(value, Mapping):
        overrides: dict[Capability, bool] = {}
        for raw_key, raw_flag in value.items():
            name = str(raw_key).strip().lower()
            if name not in {c.value for c in Capability}:
                known = ", ".join(sorted(c.value for c in Capability))
                raise ValueError(f"未知的能力：{name}；已知能力：{known}")
            overrides[Capability(name)] = bool(raw_flag)
        return None, overrides

    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"capabilities 必须是列表或映射，得到 {type(value).__name__}")

    enabled = {str(item).strip().lower() for item in value}
    unknown = enabled - {c.value for c in Capability}
    if unknown:
        known = ", ".join(sorted(c.value for c in Capability))
        raise ValueError(f"未知的能力：{', '.join(sorted(unknown))}；已知能力：{known}")

    return frozenset(Capability(name) for name in enabled), None


# --------------------------------------------------------------------------- #
# 请求与响应
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ChatRequest:
    """一次对话补全请求。字段与厂商无关。"""

    messages: Sequence[Message]
    temperature: float | None = None
    max_tokens: int | None = None
    stream: bool = False
    response_format: ResponseFormat | None = None
    #: 仅在 ``response_format="json_schema"`` 时需要；缺了它**必须报错而非降级**（FR-P-04）
    json_schema: Mapping[str, Any] | None = None
    stop: Sequence[str] | None = None
    tools: Sequence[ToolSpec] | None = None
    tool_choice: str | None = None
    timeout_s: float | None = None
    trace_id: str = ""

    def __post_init__(self) -> None:
        fmt = self.response_format
        if fmt is not None and fmt not in VALID_RESPONSE_FORMATS:
            known = ", ".join(sorted(VALID_RESPONSE_FORMATS))
            raise ValueError(f"未知的 response_format={fmt!r}；已知取值：{known}")
        # FR-P-04：不猜 schema。静默降级成无约束生成，下游 json.loads 必然失败，
        # 而失败点离原因很远。宁可在这里报错。
        if fmt == "json_schema" and self.json_schema is None:
            raise ValueError(
                "response_format='json_schema' 必须同时给出 json_schema；"
                "若不需要 schema 约束，请用 response_format='json'"
            )


@dataclass(frozen=True)
class Usage:
    """token 用量。**``None`` 表示「上游没说」，绝不用 0 代替**（FR-P-10）。

    0 和「不知道」在计量上语义完全不同：前者会让成本静默算成 0 元（报表失真），
    后者会触发「标记未知」。用类型系统强制区分，比靠注释约定可靠。
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None

    @property
    def known(self) -> bool:
        return self.input_tokens is not None or self.output_tokens is not None

    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None and self.output_tokens is None:
            return None
        return (self.input_tokens or 0) + (self.output_tokens or 0)

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=_add_opt(self.input_tokens, other.input_tokens),
            output_tokens=_add_opt(self.output_tokens, other.output_tokens),
            cached_input_tokens=_add_opt(self.cached_input_tokens, other.cached_input_tokens),
        )


def _add_opt(a: int | None, b: int | None) -> int | None:
    if a is None:
        return b
    if b is None:
        return a
    return a + b


@dataclass(frozen=True)
class ChatResponse:
    """一次对话补全的结果。"""

    content: str
    model: str
    finish_reason: str = ""
    #: 推理模型的思考过程。**独立字段，绝不混进 content**（FR-P-05）——
    #: 混进去会污染下游结构化解析，且只在推理模型上复现，开发期极难发现。
    reasoning: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = field(default_factory=Usage)
    #: 本次是否走了厂商**原生**结构化输出。False 表示按能力降级过，
    #: 下游据此判断 JSON 是「约束出来的」还是「运气好」（FR-P-04）。
    structured_native: bool = False
    #: 厂商原始响应，供排障。**已脱敏**（NFR-P-04）。
    raw: Mapping[str, Any] | None = None
    trace_id: str = ""


@dataclass(frozen=True)
class EmbeddingResult:
    """一次向量化结果。``vectors`` 与入参**必须等长**（FR-P-07）。"""

    vectors: tuple[tuple[float, ...], ...]
    model: str
    dimension: int
    usage: Usage = field(default_factory=Usage)
    trace_id: str = ""

    def __post_init__(self) -> None:
        for index, vector in enumerate(self.vectors):
            if len(vector) != self.dimension:
                raise ValueError(
                    f"第 {index} 条向量维度 {len(vector)} 与声明的 dimension={self.dimension} 不一致；"
                    "该值必须与向量库的 vector(N) 对齐，否则写入即失败"
                )


# --------------------------------------------------------------------------- #
# 模型配置
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ModelConfig:
    """``src/provider`` 视角的模型配置。

    由 :meth:`from_config` 从原始配置映射投影而来 —— 适配层因此**不需要知道配置文件的形状**，
    换配置格式时不改适配器（``foundation/provider.py`` 的设计意图）。
    """

    model: str
    provider: str = ""
    base_url: str = ""
    api_key: str = ""
    api_key_env: str = ""
    temperature: float = 0.0
    max_tokens: int = 2048
    timeout_s: float = 60.0
    #: **传输层**重试次数（FR-P-11）。默认 1，刻意取小 —— 它会与 gateway 的重试相乘。
    retries: int = 1
    backoff_s: float = 1.5
    backoff_jitter: float = 0.3
    #: **完整**能力声明（配置里写成列表时）。``None`` = 未声明。
    capabilities: frozenset[Capability] | None = None
    #: **增量**能力覆盖（配置里写成映射时）。``None`` = 未声明。
    #: 与 ``capabilities`` 至多一个非 ``None`` —— 见 :func:`_parse_capabilities`。
    capability_overrides: Mapping[Capability, bool] | None = None
    #: embedding 维度，必须与向量库对齐（FR-P-07）
    dimension: int | None = None
    #: 单次请求的条数上限。``None`` = 未配置，由适配器的 ``default_max_batch()`` 兜底
    #: —— 各家上限不同（DashScope 比 OpenAI 严），把它做成**厂商知识**而非通用默认值。
    max_batch: int | None = None
    #: 老 vLLM 等端点可能不支持 ``response_format``，配置里关掉
    supports_json_native: bool = True
    uses_json_object_mode: bool = True
    #: ``None`` = 跟随 Provider.REQUIRES_API_KEY；显式 true 表示这个模型单独收紧
    require_api_key: bool | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def capabilities_or(self, default: frozenset[Capability]) -> frozenset[Capability]:
        """把能力声明解析成最终集合。优先级：**完整声明 > 增量覆盖 > 厂商默认**（FR-P-08）。

        这条三级优先级的必要性见 :func:`_parse_capabilities` ——
        写成映射时若按「替换」解释，用户会意外丢掉厂商默认里他没提到的能力。
        """
        if self.capabilities is not None:
            return self.capabilities
        if self.capability_overrides:
            resolved = set(default)
            for capability, enabled in self.capability_overrides.items():
                if enabled:
                    resolved.add(capability)
                else:
                    resolved.discard(capability)
            return frozenset(resolved)
        return default

    @classmethod
    def from_config(
        cls,
        model_cfg: Mapping[str, Any],
        *,
        providers: Mapping[str, Any] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> ModelConfig:
        """从原始 model 配置构造，自动完成厂商段投影与凭据解析。

        Raises:
            ValueError: 缺少 ``model``；或引用了未定义的 ``provider``。
        """
        merged = project_model_config(model_cfg, providers)

        name = str(merged.get("model") or "").strip()
        if not name:
            raise ValueError("model 配置缺少 `model` 字段（厂商模型名，如 gpt-4o-mini）")

        extra = merged.get("extra") or {}
        if not isinstance(extra, Mapping):
            raise ValueError(f"extra 必须是映射，得到 {type(extra).__name__}")

        capabilities, capability_overrides = _parse_capabilities(merged.get("capabilities"))

        return cls(
            model=name,
            provider=str(merged.get("provider") or "").strip(),
            base_url=str(merged.get("base_url") or "").strip().rstrip("/"),
            api_key=resolve_api_key(merged, env=env),
            api_key_env=str(merged.get("api_key_env") or "").strip(),
            temperature=float(merged.get("temperature") or 0.0),
            max_tokens=int(merged.get("max_tokens") or 2048),
            timeout_s=float(merged.get("timeout_s") or 60.0),
            retries=int(merged.get("retries", 1)),
            backoff_s=float(merged.get("backoff_s") or 1.5),
            backoff_jitter=float(merged.get("backoff_jitter", 0.3)),
            capabilities=capabilities,
            capability_overrides=capability_overrides,
            dimension=_opt_int(merged.get("dimension")),
            max_batch=_opt_int(merged.get("max_batch")),
            supports_json_native=bool(merged.get("supports_json_native", True)),
            uses_json_object_mode=bool(merged.get("uses_json_object_mode", True)),
            require_api_key=_opt_bool(merged.get("require_api_key")),
            extra=dict(extra),
        )


def _opt_int(value: Any) -> int | None:
    return None if value is None or value == "" else int(value)


def _opt_bool(value: Any) -> bool | None:
    return None if value is None else bool(value)
