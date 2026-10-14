"""续跑判定：**「跑到一半」必须是一个可判定的状态**（`FR-A-10`）。

**为什么这组是本层最重要的测试**：在它存在之前，续跑只看检查点 ——
于是「工具执行过、结果没回填」那一步会被**安静地重做**，
而工具层的文档早就写明「那是**上层**的决定」。

这里逐条钉住三种处置，以及它们各自**默认不默认**。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from agent.state import TaskRun
from repo.checkpoint import CheckpointRepo
from repo.tool import ToolExecutionRepo
from tests.unit.agent.conftest import FakeGateway, make_runtime, policy

RUN_ID = "run-resume"


async def _seed_checkpoint(db, *, status: str = "running", idem_keys: tuple[str, ...] = ("k1",)) -> None:
    run = TaskRun(run_id=RUN_ID, stage="test_case", goal="写用例")
    run.record(node_id="test_case", summary="调用 read", tools_used=("read",), idem_keys=idem_keys)
    async with db.transaction() as tx:
        await CheckpointRepo(tx).save(
            RUN_ID,
            step_index=run.next_index,
            node_id="test_case",
            status=status,
            state=run.to_state(),
        )


async def _seed_uncertain_tool(db, *, key: str = "k1", age_minutes: int = 60) -> None:
    """登记一条 **in_flight 且已超租约** 的工具执行 —— 工具层会把它判成 ``uncertain``。"""
    async with db.transaction() as tx:
        await ToolExecutionRepo(tx).register(
            scope=RUN_ID,
            idem_key=key,
            tool_name="read",
            side_effect="read",
            args_digest="digest",
            owner="someone",
            now=datetime.now(timezone.utc) - timedelta(minutes=age_minutes),
        )


def _texts(gateway) -> str:
    out: list[str] = []
    for call in gateway.calls:
        for message in call["messages"]:
            content = message.content
            out.append(content if isinstance(content, str) else "")
    return "\n".join(out)


# ---------------------------------------------------------------- 从头跑
async def test_without_a_checkpoint_it_starts_from_scratch(db, clock):
    """**不是错误**：上次什么都没做成，从头跑就是对的（`CA-19`）。"""
    runtime = make_runtime(database=db, clock=clock)
    outcome = await runtime.run_one("帮我写测试用例", run_id=RUN_ID, resume=True)
    assert outcome.status == "done"


async def test_a_finished_run_is_not_resumed(db, clock):
    """已完成的不续跑 —— 那不叫续跑，叫重跑一个做完的任务（`CA-24`）。"""
    await _seed_checkpoint(db, status="done")
    runtime = make_runtime(database=db, clock=clock)
    outcome = await runtime.run_one("帮我写测试用例", run_id=RUN_ID, resume=True)
    assert outcome.status == "done"


async def test_resume_tells_the_model_what_is_already_done(db, clock):
    """续跑时**必须把「已经做到哪了」写进提示词** —— 否则模型会从头再做一遍。"""
    await _seed_checkpoint(db)
    gateway = FakeGateway()
    runtime = make_runtime(database=db, clock=clock, gateway=gateway)
    outcome = await runtime.run_one("帮我写测试用例", run_id=RUN_ID, resume=True)

    assert outcome.status == "done"
    assert "续跑" in _texts(gateway), "提示词里要有续跑说明"


# ---------------------------------------------------------------- 不确定
async def test_uncertain_tool_execution_needs_review_by_default(db, clock, caplog):
    """**默认最保守**（`CA-21`）：宁可停下问一句，不可默默重做。"""
    await _seed_checkpoint(db)
    await _seed_uncertain_tool(db)

    gateway = FakeGateway()
    runtime = make_runtime(database=db, clock=clock, gateway=gateway)
    outcome = await runtime.run_one("帮我写测试用例", run_id=RUN_ID, resume=True)

    assert outcome.status == "needs_review"
    assert outcome.needs_review
    assert "结果没回填" in outcome.reason
    assert "read" in outcome.reason, "理由里要指出**是哪个调用**不确定，否则人来了还得自己翻"
    assert gateway.calls == [], "**一次模型调用都不该发生**"


async def test_uncertain_can_be_configured_to_abort(db, clock):
    await _seed_checkpoint(db)
    await _seed_uncertain_tool(db)

    runtime = make_runtime(
        database=db, clock=clock, policy=policy(resume={"on_uncertain": "abort"})
    )
    outcome = await runtime.run_one("帮我写测试用例", run_id=RUN_ID, resume=True)
    assert outcome.status == "failed"
    assert "放弃续跑" in outcome.error


async def test_uncertain_can_be_configured_to_retry_and_warns(db, clock, caplog):
    """`retry_uncertain` 是"**我知道可能重复，我接受**"—— 必须显式配，且要告警。"""
    await _seed_checkpoint(db)
    await _seed_uncertain_tool(db)

    runtime = make_runtime(
        database=db, clock=clock, policy=policy(resume={"on_uncertain": "retry_uncertain"})
    )
    with caplog.at_level(logging.WARNING):
        outcome = await runtime.run_one("帮我写测试用例", run_id=RUN_ID, resume=True)

    assert outcome.status == "done"
    assert any("冒险重做" in r.getMessage() for r in caplog.records)


async def test_a_settled_tool_execution_resumes_normally(db, clock):
    """工具调用**已了结**（没超租约或已完成）→ 正常续跑。"""
    await _seed_checkpoint(db)
    await _seed_uncertain_tool(db, age_minutes=0)   # 刚登记，还没超租约

    runtime = make_runtime(database=db, clock=clock)
    outcome = await runtime.run_one("帮我写测试用例", run_id=RUN_ID, resume=True)
    assert outcome.status == "done"


async def test_missing_idem_keys_do_not_block_resume(db, clock):
    """旧快照没记幂等键（`AQ-2` 之前写的）→ **按"无记录"处理**，不拦。

    把它当成"不确定"会让**本来正常的续跑**全被拦下来 —— 那是另一种错。
    """
    await _seed_checkpoint(db, idem_keys=())
    await _seed_uncertain_tool(db)

    runtime = make_runtime(database=db, clock=clock)
    outcome = await runtime.run_one("帮我写测试用例", run_id=RUN_ID, resume=True)
    assert outcome.status == "done", "查不了就不拦 —— 说'不确定'会把正常的续跑也挡住"


async def test_a_run_left_in_needs_review_is_not_silently_resumed(db, clock):
    """上次停在「等人看」→ 先处理它，别直接盖掉那个待决的问题。"""
    await _seed_checkpoint(db, status="needs_review")
    runtime = make_runtime(database=db, clock=clock)
    outcome = await runtime.run_one("帮我写测试用例", run_id=RUN_ID, resume=True)

    assert outcome.status == "needs_review"
    assert "先处理它" in outcome.reason


# ---------------------------------------------------------------- 决策要留痕
async def test_the_decision_is_emitted_with_its_evidence(db, clock):
    """`agent.resume.decided` **必须带依据** —— 否则人来了还得自己翻是哪次调用。"""
    await _seed_checkpoint(db)
    await _seed_uncertain_tool(db)

    class _Recorder:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict]] = []

        def emit(self, name, payload):
            self.events.append((name, dict(payload)))

    recorder = _Recorder()
    runtime = make_runtime(database=db, clock=clock)
    runtime.events = recorder
    await runtime.run_one("帮我写测试用例", run_id=RUN_ID, resume=True)

    decided = [payload for name, payload in recorder.events if name == "agent.resume.decided"]
    assert decided and decided[0]["decision"] == "needs_review"
    assert decided[0]["reason"]


# ---------------------------------------------------------------- 不落库
async def test_resume_without_a_database_starts_from_scratch(clock):
    """没有数据库时**明说**"查不了快照"，而不是假装查过了。"""
    runtime = make_runtime(database=None, clock=clock)
    outcome = await runtime.run_one("帮我写测试用例", run_id=RUN_ID, resume=True)
    assert outcome.status == "done"
