"""存记忆前的三道闸门（`FR-M-11..13`）。

**为什么这组值得写**：闸门是「不该记的进不来」这条目标的全部实现，
而它失效的方式有两种方向 —— 太松会把噪声放进去（复利污染），
太严会把有用的经验挡住。**fail-closed / 短路 / 硬否决** 这三条尤其要钉死。
"""

from __future__ import annotations

import pytest

from memory.policy import GatePolicy, WriteGate
from memory.types import MemoryCandidate, MemoryContext, MemorySource

CTX = MemoryContext(scope="session")


def _candidate(text: str) -> MemoryCandidate:
    return MemoryCandidate(content=text, source=MemorySource())


def _gate(chat=None, **cfg) -> WriteGate:
    return WriteGate(chat, GatePolicy.from_config(cfg))


# ---------------------------------------------------------------- 闸门① 规则
@pytest.mark.parametrize(
    "text,reason",
    [
        ("hi", "过短"),
        ("现在几点", "临时信息"),
        ("本次请求的 trace_id 是 3f2a1b0c9d8e7f6a5b4c3d2e1f0a9b8c", "临时信息"),
        ("sk-abcdefghijklmnopqrstuvwxyz", "密钥"),
        ("AKIAIOSFODNN7EXAMPLE", "密钥"),
        ("password = hunter2xyz", "密钥"),
    ],
)
async def test_rule_gate_rejects(text, reason):
    verdict = await _gate().judge(_candidate(text), CTX)
    assert not verdict.allow
    assert verdict.gate == "rules"


async def test_secret_never_reaches_the_llm(chat):
    """密钥**绝不送进 LLM 判断** —— 判断本身就是一次外发。"""
    await _gate(chat).judge(_candidate("sk-abcdefghijklmnop"), CTX)
    assert chat.calls == []


async def test_rule_gate_rejects_disallowed_scope():
    gate = _gate(scope={"allow": ["user"]})
    verdict = await gate.judge(_candidate("本仓库用 pytest 做测试"), MemoryContext(scope="session"))
    assert not verdict.allow and verdict.gate == "rules"


async def test_rule_gate_rejects_overlong_content():
    verdict = await _gate(limits={"max_chars": 20}).judge(_candidate("长" * 50), CTX)
    assert not verdict.allow


# ---------------------------------------------------------------- 闸门② LLM
async def test_judge_fail_closed_without_model():
    """`DM-11`：判不了就不记 —— 放行一条没被判断过的记忆等于没有闸门。"""
    verdict = await _gate(None).judge(_candidate("本仓库的库名是 besa_agent"), CTX)
    assert not verdict.allow and verdict.gate == "judge"


async def test_judge_fail_closed_when_llm_raises(chat):
    chat.fail = True
    verdict = await _gate(chat).judge(_candidate("本仓库的库名是 besa_agent"), CTX)
    assert not verdict.allow and verdict.gate == "judge"


async def test_judge_rejects_noise(chat):
    chat.judge = {"should_store": False, "reason": "寒暄，不是可复用结论"}
    verdict = await _gate(chat).judge(_candidate("今天天气不错啊"), CTX)
    assert not verdict.allow and verdict.gate == "judge"


async def test_judge_carries_kind_and_confidence(chat):
    chat.judge = {"should_store": True, "kind": "preference", "confidence": 0.91}
    verdict = await _gate(chat).judge(_candidate("用例命名要带需求号"), CTX)
    assert verdict.allow and verdict.kind == "preference"
    assert verdict.confidence == pytest.approx(0.91)


async def test_judge_rejects_unknown_kind(chat):
    chat.judge = {"should_store": True, "kind": "随便编的"}
    verdict = await _gate(chat).judge(_candidate("一条内容"), CTX)
    assert not verdict.allow and verdict.gate == "judge"


async def test_judge_unavailable_can_be_configured_to_allow(chat):
    chat.fail = True
    gate = _gate(chat, gates={"llm_judge": {"on_unavailable": "allow"}})
    verdict = await gate.judge(_candidate("本仓库的库名是 besa_agent"), CTX)
    assert verdict.allow


# ---------------------------------------------------------------- 闸门③ 合规
@pytest.mark.parametrize("text", ["联系我 13812345678", "邮箱是 a.b@example.com"])
async def test_compliance_redacts_pii_by_default(chat, text):
    verdict = await _gate(chat).judge(_candidate(f"用户的偏好：{text}"), CTX)
    assert verdict.allow
    assert "13812345678" not in verdict.effective_content
    assert "已脱敏" in verdict.effective_content


async def test_compliance_rejects_pii_when_configured(chat):
    gate = _gate(chat, gates={"compliance": {"pii": "reject"}})
    verdict = await gate.judge(_candidate("联系我 13812345678"), CTX)
    assert not verdict.allow and verdict.gate == "compliance"


async def test_compliance_deny_keywords(chat):
    gate = _gate(chat, gates={"compliance": {"deny_keywords": ["机密"]}})
    verdict = await gate.judge(_candidate("这段内容是机密，请记住"), CTX)
    assert not verdict.allow and verdict.gate == "compliance"


async def test_compliance_vetoes_judge(chat):
    """合规有**硬否决权** —— 它压过闸门②的判断。"""
    chat.judge = {"should_store": True, "kind": "fact", "confidence": 1.0}
    gate = _gate(chat, gates={"compliance": {"deny_patterns": ["禁"]}})
    verdict = await gate.judge(_candidate("这条被禁记的内容"), CTX)
    assert not verdict.allow and verdict.gate == "compliance"


async def test_compliance_tenant_allowlist(chat):
    gate = _gate(chat, gates={"compliance": {"allow_tenants": ["t-1"]}})
    verdict = await gate.judge(_candidate("一条事实"), MemoryContext(tenant_id="t-2"))
    assert not verdict.allow and verdict.gate == "compliance"


# ---------------------------------------------------------------- 配置校验
def test_invalid_policy_is_rejected():
    with pytest.raises(ValueError):
        GatePolicy.from_config({"gates": {"llm_judge": {"on_unavailable": "whatever"}}})
    with pytest.raises(ValueError):
        GatePolicy.from_config({"gates": {"compliance": {"deny_patterns": ["("]}}})
    with pytest.raises(ValueError):
        GatePolicy.from_config({"kinds": {"enabled": ["nonsense"]}})
