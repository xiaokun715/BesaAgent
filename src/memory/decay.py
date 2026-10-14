"""遗忘与衰减（`FR-M-07`）。

**为什么是曲线而不是一刀切**：线性衰减会在某个固定时刻把一批记忆**同时**判死，
而「重要且常用的记忆应当留得久」这件事它表达不出来。

用的是**间隔重复式**的指数半衰期 —— 与人类遗忘曲线的形状同源：

    decay(Δt) = 0.5 ** (Δt / half_life)          # Δt 从 last_accessed_at 起算
    effective_weight = base_weight × decay(Δt) × (1 + ln(1 + evidence_count))

三个要点：

1. **访问会重置计时**（`last_accessed_at`）—— 被用到就"回血"，这是间隔重复；
2. **证据越高抗衰减越强**（``evidence_count`` 的加成）—— 被多次独立佐证的事更可信；
3. **受保护的记忆**（``preference`` / 高 ``importance``）用更长的半衰期。

**这个模块是纯函数**：给定 ``(item, now, policy)`` 结果**确定**（`NFR-M-06`），
没有随机、没有墙钟、没有 IO。所以它可以直接被单测（配合 ``FakeClock`` 推进时间），
也可以在读路径上**惰性**调用 —— 不需要为了算一个权重去开事务。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from memory.types import MemoryItem, saturating_weight

__all__ = ["DecayPolicy", "utc_from_epoch"]

_SECONDS_PER_DAY = 86_400.0


def utc_from_epoch(seconds: float) -> datetime:
    """把 ``Clock.now()`` 的 epoch 秒转成 aware datetime。

    ``Clock`` 只给 epoch 秒（``foundation/clock.py``），而记忆的时间戳是 ``datetime`` ——
    转换只应该有一处，否则「有的是 UTC、有的不是」会在跨时区部署时才暴露。
    """
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


@dataclass(frozen=True)
class DecayPolicy:
    """衰减参数（``memory.decay`` 段）。"""

    half_life_days: float = 30.0
    protected_half_life_days: float = 365.0
    tombstone_threshold: float = 0.05
    reinforce_saturate: float = 5.0
    #: 证据加成的强度。1.0 表示 ``(1 + ln(1 + evidence))``；调大让它更抗衰减。
    evidence_boost: float = 1.0
    #: 低于这个 ``importance`` 才算「不受保护」。
    protect_importance: float = 0.8

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | None) -> "DecayPolicy":
        data = dict(cfg or {})
        policy = cls(
            half_life_days=float(data.get("half_life_days", 30.0)),
            protected_half_life_days=float(data.get("protected_half_life_days", 365.0)),
            tombstone_threshold=float(data.get("tombstone_threshold", 0.05)),
            reinforce_saturate=float(data.get("reinforce_saturate", 5.0)),
            evidence_boost=float(data.get("evidence_boost", 1.0)),
            protect_importance=float(data.get("protect_importance", 0.8)),
        )
        policy._validate()
        return policy

    def _validate(self) -> None:
        if self.half_life_days <= 0:
            raise ValueError(f"memory.decay.half_life_days 必须为正，得到 {self.half_life_days}")
        if self.protected_half_life_days <= 0:
            raise ValueError(
                f"memory.decay.protected_half_life_days 必须为正，得到 {self.protected_half_life_days}"
            )
        if self.tombstone_threshold < 0:
            raise ValueError(
                f"memory.decay.tombstone_threshold 不能为负，得到 {self.tombstone_threshold}"
            )
        if self.reinforce_saturate <= 0:
            raise ValueError(
                f"memory.decay.reinforce_saturate 必须为正，得到 {self.reinforce_saturate}"
            )

    # ---------------------------------------------------------------- 判定
    def half_life_for(self, item: MemoryItem) -> float:
        """这条记忆用哪个半衰期。

        受保护记忆用 ``protected_half_life_days`` —— 用户的长期偏好如果因为
        「用得少」被忘掉，正是本模块最不该犯的错（`FR-M-07`）。
        """
        protected = item.kind in {"preference"} or item.importance >= self.protect_importance
        return self.protected_half_life_days if protected else self.half_life_days

    def effective_weight(self, item: MemoryItem, now: datetime) -> float:
        """现在的有效权重。**读路径上惰性调用它**（架构文档 §3.2）。

        Δt 从 ``last_accessed_at`` 起算；从没访问过就退到 ``created_at``，
        再没有就退到 ``updated_at`` —— 三者都没有时视为刚生成（因子 1.0）。
        """
        reference = item.last_accessed_at or item.created_at or item.updated_at
        if reference is None:
            elapsed_days = 0.0
        else:
            elapsed_days = max(0.0, (now - _aware(reference)).total_seconds() / _SECONDS_PER_DAY)

        half_life = self.half_life_for(item)
        decay = 0.5 ** (elapsed_days / half_life)
        evidence = 1.0 + self.evidence_boost * math.log1p(max(0, item.evidence_count))
        return item.weight * decay * evidence

    def next_state(self, item: MemoryItem, now: datetime) -> str:
        """按当前权重判定它该处于什么状态。

        **只把 ``active`` 推向 ``tombstone``** —— 已经退场的状态（``superseded`` /
        ``merged``）不是衰减管的，覆盖它们会把「被取代」的原因抹掉。
        """
        if item.state != "active":
            return item.state
        if item.expired(now):
            # 硬时效到了：这是 `task_state` 的正规退场通道（`DM-10`）。
            return "tombstone"
        if self.effective_weight(item, now) < self.tombstone_threshold:
            return "tombstone"
        return "active"

    def reinforced_weight(self, item: MemoryItem) -> float:
        """一次反哺后的新权重（饱和上界，`FR-M-08`）。

        **只在「被采用」时调用** —— 「被检索到」不算数，否则错误记忆会因为
        「总被取出来」而自我升权（架构文档 §3.3）。
        """
        return saturating_weight(item.weight, self.reinforce_saturate)

    def survived(self, item: MemoryItem, now: datetime) -> bool:
        """这条记忆现在还站得住吗（用于离线扫描的判定）。"""
        return self.next_state(item, now) == "active"


def _aware(moment: datetime) -> datetime:
    """确保是 aware datetime。

    SQLite（单测）回来的可能是 naive 的 —— 与 ``repo/tool.py`` 的 ``find``
    同一处坑：naive 与 aware 相减会抛 ``TypeError``，而错误信息不会告诉你
    「是数据库方言的问题」。
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment
