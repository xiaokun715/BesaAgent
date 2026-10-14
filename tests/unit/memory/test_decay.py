"""遗忘与衰减（`FR-M-07`）。

**为什么这组值得写**：衰减是**唯一会自动删掉用户数据**的机制，
而它跑在没有人看旁边的离线任务里。判错一个方向的代价不对等 ——
衰减太慢只是占地方，太快会把有用的记忆悄悄抹掉。所以每条断言都冲着
「该留的留下、该退的退场」去，并用**假时钟**把时间推到位（`NFR-M-05`）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from memory.decay import DecayPolicy, utc_from_epoch
from memory.types import MemoryItem, saturating_weight

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _item(**over) -> MemoryItem:
    base = dict(
        memory_id=uuid4(),
        kind="fact",
        content="本仓库的库名是 besa_agent",
        created_at=NOW,
        updated_at=NOW,
        weight=1.0,
    )
    base.update(over)
    return MemoryItem(**base)


def test_fresh_memory_has_base_weight():
    assert DecayPolicy().effective_weight(_item(), NOW) == pytest.approx(1.0)


def test_weight_halves_after_one_half_life():
    policy = DecayPolicy(half_life_days=30)
    assert policy.effective_weight(_item(), NOW + timedelta(days=30)) == pytest.approx(0.5)


def test_long_unused_fact_is_tombstoned():
    policy = DecayPolicy(half_life_days=30, tombstone_threshold=0.05)
    assert policy.next_state(_item(), NOW + timedelta(days=200)) == "tombstone"


def test_protected_preference_survives():
    """用户的长期偏好如果因为「用得少」被忘掉，正是本模块最不该犯的错。"""
    policy = DecayPolicy(half_life_days=30)
    preference = _item(kind="preference", content="用例命名必须带需求号")
    assert policy.next_state(preference, NOW + timedelta(days=200)) == "active"


def test_expired_task_state_is_tombstoned_regardless_of_weight():
    """``task_state`` 是「到点即错」，硬时效比衰减曲线更早生效（`DM-10`）。"""
    policy = DecayPolicy(half_life_days=365)
    task = _item(kind="task_state", content="需求分析已完成", valid_to=NOW + timedelta(days=1))
    assert policy.next_state(task, NOW + timedelta(days=2)) == "tombstone"


def test_evidence_slows_decay():
    policy = DecayPolicy(half_life_days=30)
    plain = _item()
    corroborated = _item(evidence_count=3)
    later = NOW + timedelta(days=30)
    assert policy.effective_weight(corroborated, later) > policy.effective_weight(plain, later)


def test_reinforced_weight_is_saturating_and_monotonic():
    policy = DecayPolicy(reinforce_saturate=5.0)
    weights = [policy.reinforced_weight(_item(weight=w)) for w in (0.0, 1.0, 3.0, 10.0)]
    assert weights == sorted(weights)              # 单调
    assert all(w <= policy.reinforce_saturate for w in weights)   # 有上界


def test_saturating_weight_bounds():
    assert saturating_weight(0.0, 5.0) < 5.0
    assert saturating_weight(1_000.0, 5.0) <= 5.0   # 极大输入下浮点会贴到上界
    assert saturating_weight(1.0, 0.0) == 1.0       # saturate<=0 时不动


def test_decay_is_deterministic():
    """`NFR-M-06`：给定输入，两次运行结果必须一致（回归与审计依赖它）。"""
    policy = DecayPolicy()
    item = _item()
    moment = NOW + timedelta(days=17.5)
    assert policy.effective_weight(item, moment) == policy.effective_weight(item, moment)


def test_naive_datetime_does_not_explode():
    """SQLite 回来的可能是 naive —— 相减会抛 TypeError，而报错不会告诉你是方言问题。"""
    policy = DecayPolicy(half_life_days=30)
    naive = _item(created_at=datetime(2026, 1, 1))  # 无 tzinfo
    assert policy.effective_weight(naive, NOW + timedelta(days=30)) == pytest.approx(0.5)


def test_invalid_config_is_rejected():
    with pytest.raises(ValueError):
        DecayPolicy.from_config({"half_life_days": 0})
    with pytest.raises(ValueError):
        DecayPolicy.from_config({"reinforce_saturate": -1})


def test_utc_from_epoch_returns_aware():
    moment = utc_from_epoch(1_767_225_600)
    assert moment.tzinfo is not None
    assert moment == datetime(2026, 1, 1, tzinfo=timezone.utc)
