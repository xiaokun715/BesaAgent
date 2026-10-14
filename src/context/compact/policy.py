"""多级压缩的参数（``context.linear.compact`` 段）。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from context.types import COMPRESSION_LEVELS, CompressionLevel

__all__ = ["CompactPolicy"]


@dataclass(frozen=True)
class CompactPolicy:
    """压缩参数。**每一项都有默认值，且默认值是保守的那一档**。"""

    enabled: bool = True
    #: 级数上限。**默认 L2** —— L3 丢的是时间脉络，应当是显式选择（`C-C-3`）
    max_level: CompressionLevel = "L2"
    #: 预算用到多少就升一级
    trigger_ratio: float = 0.75
    #: 摘要走哪个逻辑名（只写别名，不写厂商模型 —— `C-C-2`）
    summary_alias: str = "runtime.default"
    #: 单次请求内最多几次压缩。**必须有上界** —— 压缩自己不能成为成本的第二个来源
    max_llm_calls: int = 2
    cache: bool = True
    #: L1 截断工具输出的上限（近无损那一级）
    l1_max_tool_output_chars: int = 2_000
    #: 尾部保护：最近几轮**不参与压缩**
    keep_recent_turns: int = 8
    #: 摘要产出的 token 上限
    max_summary_tokens: int = 512
    #: 提示词版本。**必须进缓存键** —— 改了提示词还复用旧摘要，
    #: 会拿到"用旧标准压的"结果，而这件事没有任何症状。
    prompt_version: str = "v1"

    @classmethod
    def from_config(cls, cfg: Mapping[str, object] | None) -> "CompactPolicy":
        data = dict(cfg or {})
        policy = cls(
            enabled=bool(data.get("enabled", True)),
            max_level=str(data.get("max_level", "L2")),  # type: ignore[arg-type]
            trigger_ratio=float(data.get("trigger_ratio", 0.75)),
            summary_alias=str(data.get("summary_alias", "runtime.default")),
            max_llm_calls=int(data.get("max_llm_calls", 2)),
            cache=bool(data.get("cache", True)),
            l1_max_tool_output_chars=int(data.get("l1_max_tool_output_chars", 2_000)),
            keep_recent_turns=int(data.get("keep_recent_turns", 8)),
            max_summary_tokens=int(data.get("max_summary_tokens", 512)),
            prompt_version=str(data.get("prompt_version", "v1")),
        )
        policy._validate()
        return policy

    def _validate(self) -> None:
        if self.max_level not in COMPRESSION_LEVELS:
            raise ValueError(
                f"context.linear.compact.max_level 只能是 {COMPRESSION_LEVELS}，"
                f"得到 {self.max_level!r}"
            )
        if not 0.0 < self.trigger_ratio <= 1.0:
            raise ValueError(
                f"context.linear.compact.trigger_ratio 必须在 (0,1]，得到 {self.trigger_ratio}"
            )
        if self.max_llm_calls < 0:
            raise ValueError("context.linear.compact.max_llm_calls 不能为负")
        if self.keep_recent_turns < 0:
            raise ValueError("context.linear.compact.keep_recent_turns 不能为负")

    def allows(self, level: CompressionLevel) -> bool:
        """这一级被允许吗。"""
        return COMPRESSION_LEVELS.index(level) <= COMPRESSION_LEVELS.index(self.max_level)
