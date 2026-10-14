"""离线维护的调度（`FR-M-18`）。

**为什么这组值得写**：维护任务的失效是**安静的** —— 它不报错，只是不跑。
而「为什么这条记忆一直没被淘汰」在几周后才会有人问起。
所以每条断言都冲着「到点了有没有触发」去。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from memory.scheduler import MaintenancePolicy, next_run_after

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def test_due_when_never_run():
    assert MaintenancePolicy(schedule="17 3 * * *").due(None, NOW)


def test_not_due_right_after_running():
    policy = MaintenancePolicy(schedule="17 3 * * *")
    assert not policy.due(NOW, NOW)


def test_due_when_last_run_before_the_scheduled_point():
    policy = MaintenancePolicy(schedule="17 3 * * *")
    last = datetime(2025, 12, 31, 3, 17, tzinfo=timezone.utc)  # 昨天那个点
    assert policy.due(last, NOW)          # 今天 03:17 已经过了


def test_interval_based_due():
    policy = MaintenancePolicy(schedule="", interval_hours=6)
    assert policy.due(NOW - timedelta(hours=7), NOW)
    assert not policy.due(NOW - timedelta(hours=5), NOW)


def test_default_is_daily_not_never():
    """既没 schedule 也没 interval：**默认按天**，而不是永不触发。"""
    policy = MaintenancePolicy(schedule="", interval_hours=0)
    assert policy.due(None, NOW)


def test_disabled_never_due():
    assert not MaintenancePolicy(enabled=False).due(None, NOW)


def test_older_than_threshold():
    policy = MaintenancePolicy(older_than_days=7)
    assert policy.older_than(NOW) == NOW - timedelta(days=7)
    assert MaintenancePolicy(older_than_days=0).older_than(NOW) is None


def test_next_run_after_rolls_to_tomorrow():
    policy = MaintenancePolicy(schedule="17 3 * * *")
    nxt = next_run_after(policy, NOW)      # NOW 是 12:00，今天的 03:17 已过
    assert nxt == datetime(2026, 1, 2, 3, 17, tzinfo=timezone.utc)


def test_max_items_per_run_is_bounded():
    policy = MaintenancePolicy(batch_size=100, max_batches_per_run=10)
    assert policy.max_items_per_run == 1_000


def test_complex_cron_is_rejected():
    with pytest.raises(ValueError):
        MaintenancePolicy.from_config({"schedule": "*/5 * * * *"})
