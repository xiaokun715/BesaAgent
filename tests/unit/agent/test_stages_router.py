"""阶段注册表与路由（`FR-A-01..03`）。

**为什么这组值得写**：路由错了的后果是「用错阶段跑」—— 用错工具集、白烧一轮，
甚至做错事。而它的失效**不会报错**，只会让你在某次运行后问「它怎么跑到那个阶段去了」。
所以每条断言都冲着「**理由能不能被回答**」去。
"""

from __future__ import annotations

import pytest

from agent.base import SimpleStageAgent
from agent.router import StageRouter
from agent.stages import STAGE_ORDER, StageRegistry, UnknownStageError, build_stages


# ---------------------------------------------------------------- 注册表
def test_seven_builtin_stages_are_registered():
    registry = build_stages(None)
    assert registry.names() == STAGE_ORDER
    assert len(registry) == 7


def test_registry_order_is_the_process_order():
    """顺序是契约的一部分：它就是测试流程本身。"""
    assert build_stages(None).names() == (
        "requirement", "test_design", "test_case", "environment", "execution", "analysis", "defect",
    )


def test_duplicate_registration_fails_hard():
    """静默覆盖会让一个阶段悄悄失效 —— 而表现是「某些任务突然换了立场」。"""
    registry = StageRegistry()
    agent = SimpleStageAgent(stage="x", system_prompt="p")
    registry.register(agent)
    with pytest.raises(ValueError, match="重复注册"):
        registry.register(SimpleStageAgent(stage="x", system_prompt="p"))


def test_unknown_stage_lists_the_known_ones():
    registry = build_stages(None)
    with pytest.raises(UnknownStageError) as excinfo:
        registry.get("nope")
    assert "test_case" in str(excinfo.value), "错误信息里要有可用的阶段名"


def test_stage_can_be_added_by_config_alone():
    """**数据驱动**：新增阶段不改一行代码（`DA-7`）。"""
    registry = build_stages(
        {"smoke": {"enabled": True, "description": "冒烟", "prompt": "你是冒烟测试员", "keywords": ["冒烟"]}}
    )
    assert registry.has("smoke")
    assert registry.get("smoke").system_prompt == "你是冒烟测试员"


def test_disabled_stage_is_not_registered():
    registry = build_stages({"defect": {"enabled": False}})
    assert not registry.has("defect")
    assert len(registry) == 6


def test_stage_without_prompt_is_rejected():
    with pytest.raises(ValueError, match="没有提示词"):
        build_stages({"empty": {"enabled": True, "prompt": ""}})


def test_declared_tools_are_checked_against_the_registry(caplog):
    """配置错误在**启动期**暴露（`C-A-5`），而不是等模型调到一个不存在的工具。"""
    import logging

    with caplog.at_level(logging.WARNING):
        build_stages(
            {"test_case": {"tools": ["read", "nonexistent_tool"]}},
            known_tools=frozenset({"read", "write"}),
        )
    assert any("nonexistent_tool" in record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------- 路由：显式
async def test_explicit_stage_always_wins():
    """**显式永远优先** —— 替调用方"纠正"它，是这类系统里最让人失去信任的聪明。"""
    router = StageRouter(build_stages(None), on_unknown="ask")
    decision = await router.route("今天天气怎么样，随便聊聊", stage="defect")
    assert decision.stage == "defect"
    assert decision.confidence == 1.0
    assert "显式" in decision.reason


async def test_explicit_unknown_stage_is_rejected():
    router = StageRouter(build_stages(None), on_unknown="ask")
    with pytest.raises(ValueError, match="显式指定的阶段"):
        await router.route("任务", stage="never-existed")


# ---------------------------------------------------------------- 路由：规则
@pytest.mark.parametrize(
    "task,expected",
    [
        ("帮我为登录接口写测试用例", "test_case"),
        ("分析一下这些失败日志的根因", "analysis"),
        ("梳理一下这份需求文档的验收标准", "requirement"),
        ("先做一下等价类和边界值的设计", "test_design"),
        ("把这个服务的依赖和中间件准备好", "environment"),
        ("把用例跑一遍拿到执行结果", "execution"),
        ("把这些失败整理成缺陷单", "defect"),
    ],
)
async def test_rule_routing_picks_the_right_stage(task, expected):
    decision = await StageRouter(build_stages(None), on_unknown="ask").route(task)
    assert decision.stage == expected
    assert decision.confidence > 0


async def test_routing_always_carries_a_reason():
    """**没有理由的路由在排障时等于没有信息。**"""
    decision = await StageRouter(build_stages(None), on_unknown="ask").route("帮我写测试用例")
    assert decision.reason and "关键词" in decision.reason


async def test_many_keywords_do_not_penalize_a_stage():
    """分母用**固定饱和数**而不是关键词总数 —— 否则"关键词写得越细越难被选中"。

    这是实现期发现的一个真问题：``test_case`` 有 6 个关键词，
    命中「用例」「测试用例」两个却只拿到 0.33 分而落选。
    """
    decision = await StageRouter(build_stages(None), on_unknown="ask").route("帮我写测试用例")
    assert decision.stage == "test_case"
    assert decision.confidence >= 0.5


async def test_ambiguous_task_is_not_guessed():
    """推断不出来时**不许瞎猜**（`MA-11`）。"""
    decision = await StageRouter(build_stages(None), on_unknown="ask").route("今天天气怎么样")
    assert decision.needs_input
    assert decision.stage == ""


# ---------------------------------------------------------------- 路由：兜底
async def test_on_unknown_default_uses_the_fallback_and_warns(caplog):
    import logging

    router = StageRouter(build_stages(None), on_unknown="default", default_stage="requirement")
    with caplog.at_level(logging.WARNING):
        decision = await router.route("今天天气怎么样")
    assert decision.stage == "requirement"
    assert "推断不出" in decision.reason, "用兜底跑 ≠ 判断出就是这个阶段，理由里要写清"
    assert caplog.records, "兜底**必须告警**"


async def test_on_unknown_reject_raises():
    router = StageRouter(build_stages(None), on_unknown="reject")
    with pytest.raises(ValueError, match="无法路由"):
        await router.route("今天天气怎么样")


def test_on_unknown_has_no_default():
    """`C-A-1`：逼配置者决定"猜还是问"。"""
    with pytest.raises(TypeError):
        StageRouter(build_stages(None))  # type: ignore[call-arg]


def test_default_policy_requires_a_valid_default_stage():
    with pytest.raises(ValueError, match="default_stage"):
        StageRouter(build_stages(None), on_unknown="default", default_stage="")
    with pytest.raises(ValueError, match="不在已注册的阶段里"):
        StageRouter(build_stages(None), on_unknown="default", default_stage="nope")


# ---------------------------------------------------------------- 路由：LLM（可选）
class _FakeChat:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0

    async def complete(self, **kwargs):
        self.calls += 1
        return self.reply


async def test_llm_routing_is_used_when_rules_fail():
    chat = _FakeChat('{"stage": "defect", "confidence": 0.9, "reason": "要提单"}')
    router = StageRouter(build_stages(None), on_unknown="ask", use_llm=True, chat=chat)
    decision = await router.route("把这个转出去")
    assert decision.stage == "defect"
    assert chat.calls == 1


async def test_low_confidence_llm_falls_back_to_the_policy():
    """低置信度**不采用** —— 拿一个"可能对"的结果当结论，比说"不知道"更糟。"""
    chat = _FakeChat('{"stage": "defect", "confidence": 0.1}')
    router = StageRouter(build_stages(None), on_unknown="ask", use_llm=True, chat=chat)
    decision = await router.route("把这个转出去")
    assert decision.needs_input


async def test_llm_failure_degrades_to_the_policy():
    """LLM 判断是**可选的增强**，失败不该成为必需路径（`MA-10`）。"""

    class _Boom:
        async def complete(self, **kwargs):
            raise RuntimeError("模型挂了")

    router = StageRouter(build_stages(None), on_unknown="ask", use_llm=True, chat=_Boom())
    decision = await router.route("把这个转出去")
    assert decision.needs_input


async def test_rules_run_before_the_llm():
    """能零成本解决的就不要花钱 —— 与压缩的 L1 排在 L2 前同一个思路。"""
    chat = _FakeChat('{"stage": "defect", "confidence": 1.0}')
    router = StageRouter(build_stages(None), on_unknown="ask", use_llm=True, chat=chat)
    decision = await router.route("帮我写测试用例")
    assert decision.stage == "test_case"
    assert chat.calls == 0
