"""**多级上下文压缩**（`FR-C-03..06`）—— 独立文件夹，两条策略共用。

**为什么它自己一个包**（`MC-15` / `DC-4`）：

> 多级压缩**不是策略一的私有实现**，而是两条策略共用的零件。

策略二的「会话摘要链」自己会变长 —— 那时对它做的就是这里的 **L3**。
写成两份，两份会在阈值、切分点、标记格式上**慢慢漂移**，
而漂移的表现是"同一段历史在两个地方被压得不一样"，几乎无法从症状反推。

四个零件各自的职责：

========================  ==========================================================
``boundaries``            切分点：**按「轮」**，不许切断工具调用与结果的配对
``levels``                级联：L1 零成本先做 → L2 摘要 → L3 要点，按需升级
``summarizer``            调 LLM 产出摘要（走 ``ChatModel`` 协议，失败降级）
``cache`` / ``ledger``    缓存（否则每轮重压）与账本（否则丢了什么没人知道）
========================  ==========================================================
"""

from __future__ import annotations

from context.compact.boundaries import by_step, by_turn, fingerprint, flatten, split_tail, validate_pairing
from context.compact.cache import CompactCache
from context.compact.ledger import Ledger
from context.compact.levels import Compactor, denoise
from context.compact.policy import CompactPolicy
from context.compact.summarizer import Summarizer
from context.compact.types import CompactResult, CompactUnit, blocks_from_messages, summary_block

__all__ = [
    "CompactCache",
    "CompactPolicy",
    "CompactResult",
    "CompactUnit",
    "Compactor",
    "Ledger",
    "Summarizer",
    "blocks_from_messages",
    "by_step",
    "by_turn",
    "denoise",
    "fingerprint",
    "flatten",
    "split_tail",
    "summary_block",
    "validate_pairing",
]
