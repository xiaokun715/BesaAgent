"""离线维护的调度（`FR-M-18`）。

**为什么不用外部 cron**：维护的时机是**可计算的**（「距上次多久」「到没到那个点」），
把它做成一个纯函数，就能用 ``FakeClock`` 推到任意时刻来测（`NFR-M-05`）——
而依赖外部 cron 的话，这条路径在测试里根本跑不到。

**单次运行有界**（`batch_size × max_batches_per_run`）：一个百万条记忆的库，
若一轮无界地扫，维护任务会**永远跑不完**，而且每一轮都从头上开始。
配合游标（``memory_op_log`` 里最新的 ``decay`` / ``reflect`` 时刻），
中断再跑也不重复处理已处理的批次（`CM-26`）。

**不占用在线限流额度**（`C-M-7`）：维护的 LLM 调用走独立预算 ——
它不该和正在进行的会话抢 `FR-G-06` 的名额。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any

__all__ = ["MaintenancePolicy", "next_run_after"]


@dataclass(frozen=True)
class MaintenancePolicy:
    """维护参数（``memory.maintenance`` 段）。"""

    enabled: bool = True
    #: cron 形式（**只支持 ``M H * * *`` 这种「每天某个时刻」的形式**，见下）。
    schedule: str = ""
    #: 间隔小时数。与 ``schedule`` 二选一，``schedule`` 优先。
    interval_hours: float = 0.0
    #: 单批处理条数上界。
    batch_size: int = 500
    #: 单次运行的批次上界。
    max_batches_per_run: int = 40
    #: 「长期无人访问」的门槛：超过这么多天没被访问，才进入衰减扫描的视野。
    older_than_days: float = 0.0

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | None) -> "MaintenancePolicy":
        data = dict(cfg or {})
        policy = cls(
            enabled=bool(data.get("enabled", True)),
            schedule=str(data.get("schedule", "") or ""),
            interval_hours=float(data.get("interval_hours", 0.0) or 0.0),
            batch_size=int(data.get("batch_size", 500)),
            max_batches_per_run=int(data.get("max_batches_per_run", 40)),
            older_than_days=float(data.get("older_than_days", 0.0) or 0.0),
        )
        policy._validate()
        return policy

    def _validate(self) -> None:
        if self.batch_size <= 0 or self.max_batches_per_run <= 0:
            raise ValueError("memory.maintenance 的 batch_size / max_batches_per_run 必须为正")
        if self.schedule and _parse_daily(self.schedule) is None:
            raise ValueError(
                f"memory.maintenance.schedule 只支持「分 时 * * *」形式（如 '0 3 * * *'），"
                f"得到 {self.schedule!r}。复杂表达式请用 interval_hours。"
            )

    @property
    def max_items_per_run(self) -> int:
        return self.batch_size * self.max_batches_per_run

    def due(self, last_run: datetime | None, now: datetime) -> bool:
        """现在该跑一轮维护吗。"""
        if not self.enabled:
            return False

        daily = _parse_daily(self.schedule) if self.schedule else None
        if daily is not None:
            scheduled = _most_recent_daily(daily, now)
            return last_run is None or last_run < scheduled

        if self.interval_hours > 0:
            if last_run is None:
                return True
            return (now - last_run) >= timedelta(hours=self.interval_hours)

        # 既没 schedule 也没 interval：**默认按天**，而不是「永不触发」——
        # 一个开了维护却没配时机的库，不该安静地永远不维护。
        scheduled = _most_recent_daily(time(3, 0), now)
        return last_run is None or last_run < scheduled

    def older_than(self, now: datetime) -> datetime | None:
        """衰减扫描的「长期无人访问」门槛对应的时刻。"""
        if self.older_than_days <= 0:
            return None
        return now - timedelta(days=self.older_than_days)


def next_run_after(policy: MaintenancePolicy, now: datetime) -> datetime | None:
    """下一次应该跑的时刻（用于展示与 ``doctor``）。"""
    daily = _parse_daily(policy.schedule) if policy.schedule else time(3, 0)
    if daily is None:
        if policy.interval_hours > 0:
            return now + timedelta(hours=policy.interval_hours)
        return None
    candidate = _most_recent_daily(daily, now)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def _parse_daily(expression: str) -> time | None:
    """解析 ``M H * * *``。解析不了返回 ``None``（**不猜**）。"""
    fields = expression.split()
    if len(fields) != 5:
        return None
    minute, hour, dom, month, dow = fields
    if dom != "*" or month != "*" or dow != "*":
        return None
    try:
        return time(int(hour), int(minute))
    except ValueError:
        return None


def _most_recent_daily(moment: time, now: datetime) -> datetime:
    """``now`` 当天（或前一天）最接近的那个 ``moment`` 时刻。"""
    aware = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    candidate = aware.replace(hour=moment.hour, minute=moment.minute, second=0, microsecond=0)
    if candidate > aware:
        candidate -= timedelta(days=1)
    return candidate
