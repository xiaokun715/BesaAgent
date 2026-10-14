"""存记忆前的**三道闸门**（`FR-M-11..13`）。

**为什么闸门必须有、且必须在写入之前**：记忆的错误不像工具那样发生一次就结束 ——
它会**复利**：被检索、被采用、被强化，越用越"真"（架构文档 §0）。
一旦写进去并且被强化过，事后清理它的成本（以及它已经造成的污染）都无法回退。

所以三道闸门叠加：

============================  ====================================================  ==========
闸门                           拦什么                                                 代价
============================  ====================================================  ==========
① ``RuleGate``                 格式/长度/明显的临时信息/密钥形态/作用域门禁            零（纯正则）
② ``LlmJudgeGate``             「这是不是可复用的稳定结论」+ 归哪一类 + 初始置信度     一次轻量补全
③ ``ComplianceGate``           PII / 跨租户 / 禁记类别 —— **硬否决，不可跳过**         零（纯正则）
============================  ====================================================  ==========

**顺序是约束，不是建议**：前一道拒了就**不进**下一道（短路求值）。
③ 排在最后不是因为它贵（它也便宜），而是因为它是**权威否决** ——
它的结论要压过 ② 的判断，所以必须最后过一遍。

**fail-closed**（`DM-11`）：② 的 LLM 不可用、或返回的内容解析不出来时，
**按拒绝处理**，绝不"默认记下来"。放行一条没被判断过的记忆，等于没有闸门。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from memory.types import (
    MEMORY_KINDS,
    ChatModel,
    GateVerdict,
    MemoryCandidate,
    MemoryContext,
)

__all__ = ["GatePolicy", "WriteGate"]

_log = logging.getLogger(__name__)

#: 明显的**临时信息**形态：一旦记下来就是噪声（`FR-M-10`）。
_EPHEMERAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b[0-9a-f]{32}\b", re.I),               # trace_id 那样的 32 位 hex
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),  # uuid
    re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?"),                              # 时刻
    re.compile(r"(现在几点|当前时间|本次请求|这一次调用|this request|right now)", re.I),
)
# ⚠ **刻意不拦「一串数字」**：``\d{10,13}`` 会把手机号、银行卡号一起当成
# 「epoch 时间戳」拒掉 —— 而它们是 PII，该走合规闸门的脱敏/拒绝通道，
# 不该在这里被当成临时信息悄悄丢掉（两者的处置方式完全不同）。

#: **密钥/凭据**形态。命中直接拒绝 —— 绝不把这种东西送去 LLM 判断。
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),               # OpenAI 风格
    re.compile(r"\bAKIA[0-9A-Z]{12,}"),                   # AWS Access Key
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{16,})\b"),       # GitHub token
    re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|token)\s*[:=]\s*\S{6,}"),
)

#: PII 形态。按 ``compliance.pii`` 策略决定拒绝还是脱敏。
_PII_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),                          # 手机号
    re.compile(r"\b[\w.\-+]+@[\w\-]+\.[\w.\-]+\b"),                    # 邮箱
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),                          # 身份证
    re.compile(r"(?<!\d)\d{16,19}(?!\d)"),                             # 银行卡
)

_JUDGE_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "should_store": {"type": "boolean"},
        "kind": {"type": "string", "enum": list(MEMORY_KINDS)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
    },
    "required": ["should_store"],
}

_JUDGE_SYSTEM = (
    "你是记忆写入的判定器。只回答一个问题：这段信息值得作为**跨会话可复用**的记忆保存吗？\n"
    "值得记的只有四类：\n"
    "- fact：稳定事实（长期成立、可验证）\n"
    "- preference：用户偏好（对做法/风格的稳定倾向）\n"
    "- task_state：任务状态（当前进度，会过期）\n"
    "- lesson：经验教训（这次学到的做法）\n"
    "不该记的三类：临时信息（转瞬即变）、未验证事实（猜测/未证实）、口头噪声（寒暄/情绪/闲聊）。\n"
    "判据：**它在下一次、另一个会话里，仍然成立且有用吗？** 不满足就不记。\n"
    "宁可漏记，不可错记。只输出 JSON。"
)


@dataclass(frozen=True)
class GatePolicy:
    """三道闸门的参数（``memory.gates`` 段）。"""

    # ① 规则初筛
    min_chars: int = 4
    max_chars: int = 2_000
    allow_scopes: tuple[str, ...] = ("session",)
    enabled_kinds: tuple[str, ...] = MEMORY_KINDS

    # ② LLM 判断
    judge_enabled: bool = True
    judge_max_tokens: int = 256
    #: 判断不可用时的行为。**没有默认值是配置层的事，这里给 fail-closed 的兜底。**
    on_unavailable: Literal["reject", "allow"] = "reject"

    # ③ 安全合规
    pii: Literal["reject", "redact", "allow"] = "redact"
    deny_keywords: tuple[str, ...] = ()
    #: 额外的禁记正则（配置里给字符串）
    deny_patterns: tuple[str, ...] = ()
    #: 允许写入的租户。空 = 不限（由 ctx 自己保证一致）。
    allow_tenants: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | None) -> "GatePolicy":
        """从**整个 ``memory:`` 段**构造。

        三道闸门的参数在 ``memory.gates.*`` 下（与 ``configs/base.yaml`` 一致）；
        为了单测方便，也接受把 ``rules`` / ``llm_judge`` / ``compliance``
        直接放在顶层 —— **两层都找不到才用默认值**。
        """
        data = dict(cfg or {})
        gates = _mapping(data.get("gates"))
        rules = _mapping(gates.get("rules") or data.get("rules"))
        judge = _mapping(gates.get("llm_judge") or data.get("llm_judge"))
        compliance = _mapping(gates.get("compliance") or data.get("compliance"))
        kinds = _mapping(data.get("kinds"))
        limits = _mapping(data.get("limits"))

        policy = cls(
            min_chars=int(limits.get("min_chars", 4)),
            max_chars=int(limits.get("max_chars", 2_000)),
            allow_scopes=tuple(str(x) for x in _as_seq(_mapping(data.get("scope")).get("allow")) or ("session",)),
            enabled_kinds=tuple(str(x) for x in _as_seq(kinds.get("enabled")) or MEMORY_KINDS),
            judge_enabled=bool(judge.get("enabled", True)),
            judge_max_tokens=int(judge.get("max_tokens", 256)),
            on_unavailable=str(judge.get("on_unavailable", "reject")),  # type: ignore[arg-type]
            pii=str(compliance.get("pii", "redact")),  # type: ignore[arg-type]
            deny_keywords=tuple(str(x) for x in _as_seq(compliance.get("deny_keywords"))),
            deny_patterns=tuple(str(x) for x in _as_seq(compliance.get("deny_patterns"))),
            allow_tenants=tuple(str(x) for x in _as_seq(compliance.get("allow_tenants"))),
        )
        policy._validate()
        return policy

    def _validate(self) -> None:
        if self.min_chars < 0 or self.max_chars <= self.min_chars:
            raise ValueError(
                f"memory.gates.limits 的长度区间非法：min={self.min_chars} max={self.max_chars}"
            )
        if self.on_unavailable not in ("reject", "allow"):
            raise ValueError(
                f"memory.gates.llm_judge.on_unavailable 只能是 reject|allow，"
                f"得到 {self.on_unavailable!r}"
            )
        if self.pii not in ("reject", "redact", "allow"):
            raise ValueError(f"memory.gates.compliance.pii 只能是 reject|redact|allow，得到 {self.pii!r}")
        unknown = set(self.enabled_kinds) - set(MEMORY_KINDS)
        if unknown:
            raise ValueError(
                f"memory.kinds.enabled 里有未知的种类：{', '.join(sorted(unknown))}；"
                f"已知：{', '.join(MEMORY_KINDS)}"
            )
        for pattern in self.deny_patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"memory.gates.compliance.deny_patterns 里的 {pattern!r} 不是合法正则：{exc}") from exc


class WriteGate:
    """三道闸门串成的一道门。**这是 ``manager`` 唯一调用的判定入口。**"""

    def __init__(self, chat: ChatModel | None, policy: GatePolicy) -> None:
        self._chat = chat
        self._policy = policy

    async def judge(self, candidate: MemoryCandidate, ctx: MemoryContext) -> GateVerdict:
        """短路求值：规则 → LLM → 合规。任一拒绝即返回。"""
        # ① 规则初筛（零成本）
        verdict = self._rules(candidate, ctx)
        if not verdict.allow:
            return verdict

        # ② LLM 判断（一次轻量补全；不可用则 fail-closed）
        judged = await self._judge(candidate, ctx)
        if not judged.allow:
            return judged

        # ③ 安全合规对账（硬否决，压过 ② 的判断）
        compliance = self._compliance(candidate, ctx)
        if not compliance.allow:
            return compliance

        # 把三道闸门的结论合成一个：种类与置信度取 ②，内容取 ③ 的脱敏结果。
        return GateVerdict(
            allow=True,
            gate="compliance",
            reason=judged.reason,
            kind=judged.kind or candidate.kind_hint,
            confidence=judged.confidence if judged.confidence is not None else candidate.confidence,
            content=compliance.content,
        )

    # ---------------------------------------------------------------- ① 规则
    def _rules(self, candidate: MemoryCandidate, ctx: MemoryContext) -> GateVerdict:
        text = candidate.content.strip()
        policy = self._policy

        if len(text) < policy.min_chars:
            return _reject("rules", f"内容过短（{len(text)} < {policy.min_chars} 字符），多半不构成一条记忆")
        if len(text) > policy.max_chars:
            return _reject(
                "rules",
                f"内容过长（{len(text)} > {policy.max_chars} 字符），多半是整段原文而不是一条结论",
            )
        if ctx.scope not in policy.allow_scopes:
            return _reject(
                "rules",
                f"作用域 {ctx.scope!r} 未在 memory.scope.allow 中启用（当前：{', '.join(policy.allow_scopes) or '无'}）",
            )
        for pattern in _EPHEMERAL_PATTERNS:
            if pattern.search(text):
                return _reject("rules", f"像临时信息（命中 {pattern.pattern[:32]!r}），记下来就是噪声")
        for pattern in _SECRET_PATTERNS:
            if pattern.search(text):
                # 密钥**绝不送去 LLM 判断** —— 判断本身就是一次外发。
                return _reject("rules", "命中密钥/凭据形态，直接拒绝（不送入 LLM 判断）")
        return GateVerdict(allow=True, gate="rules")

    # ---------------------------------------------------------------- ② LLM
    async def _judge(self, candidate: MemoryCandidate, ctx: MemoryContext) -> GateVerdict:
        policy = self._policy
        if not policy.judge_enabled:
            # 显式关掉判断 = 明确接受"不过滤"，不是静默降级。
            return GateVerdict(allow=True, gate="judge", reason="LLM 判断已关闭（配置）")
        if self._chat is None:
            return self._unavailable("没有注入 ChatModel")

        try:
            raw = await self._chat.complete(
                system=_JUDGE_SYSTEM,
                prompt=candidate.content,
                json_schema=_JUDGE_SCHEMA,
                max_tokens=policy.judge_max_tokens,
            )
            data = json.loads(raw)
        except Exception as exc:  # noqa: BLE001 — 任何失败都按"判断不可用"处理
            return self._unavailable(f"LLM 判断失败：{exc}")

        if not isinstance(data, Mapping):
            return self._unavailable("LLM 判断返回的不是 JSON 对象")

        if not data.get("should_store"):
            return _reject(
                "judge",
                f"判为不该记：{data.get('reason') or '不是可复用的稳定结论'}"
                "（临时信息 / 未验证事实 / 口头噪声）",
            )

        kind = data.get("kind")
        if kind not in self._policy.enabled_kinds:
            return _reject(
                "judge",
                f"判出的种类 {kind!r} 未在 memory.kinds.enabled 中启用",
            )

        confidence = data.get("confidence")
        try:
            confidence_value = float(confidence) if confidence is not None else 0.5
        except (TypeError, ValueError):
            confidence_value = 0.5
        return GateVerdict(
            allow=True,
            gate="judge",
            reason=str(data.get("reason") or ""),
            kind=kind,  # type: ignore[arg-type]
            confidence=min(1.0, max(0.0, confidence_value)),
        )

    def _unavailable(self, why: str) -> GateVerdict:
        """判断不可用 —— fail-closed（`DM-11`）或按配置放行。"""
        if self._policy.on_unavailable == "allow":
            _log.warning("记忆判断不可用，按配置放行：%s", why)
            return GateVerdict(allow=True, gate="judge", reason=f"判断不可用但配置为放行：{why}")
        return _reject("judge", f"判断不可用，按 fail-closed 拒绝：{why}")

    # ---------------------------------------------------------------- ③ 合规
    def _compliance(self, candidate: MemoryCandidate, ctx: MemoryContext) -> GateVerdict:
        policy = self._policy
        text = candidate.content

        if policy.allow_tenants and ctx.tenant_id not in policy.allow_tenants:
            return _reject(
                "compliance",
                f"租户 {ctx.tenant_id!r} 不在允许写入的名单里",
            )

        for keyword in policy.deny_keywords:
            if keyword and keyword in text:
                return _reject("compliance", f"命中配置的禁记关键词：{keyword!r}")
        for pattern in policy.deny_patterns:
            if re.search(pattern, text):
                return _reject("compliance", f"命中配置的禁记正则：{pattern!r}")

        hits = [pattern for pattern in _PII_PATTERNS if pattern.search(text)]
        if hits and policy.pii == "reject":
            return _reject("compliance", f"命中 PII 形态 ×{len(hits)}，策略为拒绝")
        if hits and policy.pii == "redact":
            return GateVerdict(
                allow=True,
                gate="compliance",
                reason=f"PII 命中 ×{len(hits)}，已脱敏后入库",
                content=_redact(text),
            )
        return GateVerdict(allow=True, gate="compliance")

    @property
    def policy(self) -> GatePolicy:
        return self._policy


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def _reject(gate: str, reason: str) -> GateVerdict:
    return GateVerdict(allow=False, gate=gate, reason=reason)  # type: ignore[arg-type]


def _redact(text: str) -> str:
    redacted = text
    for pattern in _PII_PATTERNS:
        redacted = pattern.sub("[已脱敏]", redacted)
    return redacted


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_seq(value: Any) -> Sequence[Any]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        return (value,)
    if isinstance(value, Sequence):
        return value
    return (value,)
