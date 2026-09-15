"""成本核算：价格表 × 用量。

**三个必须区分的状态**（``FR-G-09``）：

======================================  ==========================================
情况                                     返回
======================================  ==========================================
该模型没配价格                            ``Cost(amount=None)`` —— **未知**
上游没返回用量                            ``Cost(amount=None)`` —— **未知**
有价格也有用量                            实际金额
======================================  ==========================================

**绝不返回 0**。0 元是「免费」，``None`` 是「不知道」。
把后者写成前者，会让成本报表**静默失真** —— 总成本看起来很低，
但没人知道低是因为省钱还是因为漏算。这类错误一旦进入报表就再也追不回来了。

**价格表外置**（``D-6``）：单价会变，且不同供应商不同。改价是改配置，
不是改代码、更不是发版。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from gateway.types import Cost
from provider.types import Usage

__all__ = ["CostSheet", "Price"]

_log = logging.getLogger(__name__)

#: 价格表的计价单位：每 1K token。写进代码而不是配置 ——
#: 单位混用（有的厂商按 1K 报、有的按 1M 报）是成本计算出错的头号原因，
#: 钉死一个单位，折算发生在**填价格表的时候**，那里能看见原始报价。
PRICE_UNIT = Decimal(1000)


@dataclass(frozen=True)
class Price:
    """单个模型的价格（每 1K token）。"""

    input: Decimal
    output: Decimal
    #: 命中缓存的输入价格。``None`` 表示厂商不支持缓存计价 ——
    #: 此时缓存命中的 token 按正常输入价算（保守，不会低估成本）。
    cached_input: Decimal | None = None

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any]) -> Price:
        return cls(
            input=_decimal(cfg.get("input"), "input"),
            output=_decimal(cfg.get("output"), "output"),
            cached_input=None if cfg.get("cached_input") is None else _decimal(cfg.get("cached_input"), "cached_input"),
        )


class CostSheet:
    """价格表。"""

    def __init__(
        self,
        prices: Mapping[str, Price] | None = None,
        *,
        currency: str = "CNY",
    ) -> None:
        self._prices: dict[str, Price] = dict(prices or {})
        self.currency = currency

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | None) -> CostSheet:
        data = dict(cfg or {})
        raw_prices = data.get("prices") or {}
        prices = {key: Price.from_config(value) for key, value in raw_prices.items()}
        return cls(prices, currency=str(data.get("currency", "CNY")))

    # ---------------------------------------------------------------- 查询
    def price_of(self, model_key: str) -> Price | None:
        return self._prices.get(model_key)

    def knows(self, model_key: str) -> bool:
        """供路由的「成本优先」策略使用 —— 不知道价格的模型不参与比价。"""
        return model_key in self._prices

    # ---------------------------------------------------------------- 计算
    def calc(self, model_key: str, usage: Usage) -> Cost:
        """计算一次调用的成本。

        Args:
            model_key: 模型键（与配置里的 ``models:`` 一致）。
            usage: 用量。字段为 ``None`` 表示上游未返回。

        Returns:
            :class:`Cost`\ 。价格未知或用量未知时 ``amount is None``。
        """
        price = self._prices.get(model_key)
        if price is None:
            return Cost(currency=self.currency, amount=None)

        if usage.input_tokens is None and usage.output_tokens is None:
            # 价格知道但用量不知道 —— 同样只能标未知。
            # 想「估算」的话应该显式提供 tokenizer（D-5 决定一期不做）。
            return Cost(currency=self.currency, amount=None)

        input_tokens = usage.input_tokens or 0
        output_tokens = usage.output_tokens or 0
        cached = usage.cached_input_tokens or 0

        # 缓存命中的 token **是输入的一部分**（上游报的 prompt_tokens 含它们），
        # 所以要先把它们从原价部分扣掉，否则会重复计费。
        cached = min(cached, input_tokens)
        fresh_input = input_tokens - cached

        cached_price = price.cached_input if price.cached_input is not None else price.input

        amount = (
            Decimal(fresh_input) * price.input
            + Decimal(cached) * cached_price
            + Decimal(output_tokens) * price.output
        ) / PRICE_UNIT

        return Cost(currency=self.currency, amount=amount)

    def __repr__(self) -> str:
        return f"CostSheet(currency={self.currency!r}, models={sorted(self._prices)})"


def _decimal(value: Any, field_name: str) -> Decimal:
    if value is None:
        raise ValueError(f"价格字段 {field_name} 不能为空")
    # 用 str 中转而不是直接 Decimal(float)：后者会把 0.001 变成
    # 0.001000000000000000020816681711721685... 而价格表就该是精确的十进制。
    try:
        return Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - 统一转成配置错误
        raise ValueError(f"价格字段 {field_name}={value!r} 不是合法数字") from exc
