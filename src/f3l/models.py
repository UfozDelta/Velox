"""Plain data types shared by the exchange client, order planners and CLI."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any, Optional

BUY = "buy"
SELL = "sell"
SIDES = (BUY, SELL)


def D(value: Any) -> Decimal:
    """Convert anything number-like to Decimal without float artefacts."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def fmt(value: Optional[Decimal]) -> str:
    """Format a Decimal without scientific notation or trailing zeros."""
    if value is None:
        return ""
    text = format(D(value), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def opposite(side: str) -> str:
    return SELL if side == BUY else BUY


@dataclass(frozen=True)
class Contract:
    """Trading rules for one futures contract."""

    symbol: str
    tick_size: Decimal
    lot_size: int
    multiplier: Decimal
    is_inverse: bool = False
    max_leverage: Optional[int] = None
    max_order_lots: Optional[int] = None

    def round_price(self, price: Any, side: Optional[str] = None) -> Decimal:
        """Snap a price to the tick grid.

        Buys round down and sells round up, so rounding never makes a limit
        order more aggressive than requested. With no side, round to nearest.
        """
        mode = {BUY: ROUND_FLOOR, SELL: ROUND_CEILING}.get(side, ROUND_HALF_UP)
        ticks = (D(price) / self.tick_size).to_integral_value(rounding=mode)
        return (ticks * self.tick_size).quantize(self.tick_size)

    def lot_value(self, price: Any) -> Decimal:
        """USD value of one lot at ``price``."""
        if self.is_inverse:
            return abs(self.multiplier)
        return D(price) * abs(self.multiplier)

    def usd_to_lots(self, usd: Any, price: Any) -> int:
        """Largest whole number of lots (multiple of lot_size) worth <= usd."""
        value = self.lot_value(price)
        if value <= 0:
            raise ValueError(f"cannot size an order at price {price}")
        lots = int((D(usd) / value).to_integral_value(rounding=ROUND_FLOOR))
        return lots - lots % self.lot_size

    def lots_to_usd(self, lots: int, price: Any) -> Decimal:
        return self.lot_value(price) * lots


@dataclass(frozen=True)
class Ticker:
    symbol: str
    bid: Decimal
    ask: Decimal
    last: Decimal


@dataclass(frozen=True)
class OrderStatus:
    """The parts of an order we need to track fills."""

    order_id: str
    lots: int
    filled_lots: int
    filled_value: Decimal
    is_active: bool

    @property
    def remaining(self) -> int:
        return self.lots - self.filled_lots


@dataclass
class OrderRequest:
    """One order, in exchange-neutral terms. Size is always in lots."""

    side: Optional[str]
    symbol: str
    lots: Optional[int] = None
    type: str = "limit"
    price: Optional[Decimal] = None
    leverage: Optional[int] = None
    post_only: bool = False
    hidden: bool = False
    iceberg: bool = False
    visible_lots: Optional[int] = None
    reduce_only: bool = False
    close_order: bool = False
    time_in_force: Optional[str] = None
    stop: Optional[str] = None  # "up" or "down"
    stop_price: Optional[Decimal] = None
    stop_price_type: str = "MP"
    take_profit: Optional[Decimal] = None
    stop_loss: Optional[Decimal] = None
    margin_mode: Optional[str] = None
    client_oid: str = field(default_factory=lambda: uuid.uuid4().hex)

    def validate(self) -> None:
        if self.side is not None and self.side not in SIDES:
            raise ValueError(f"side must be buy or sell, got {self.side!r}")
        if self.type not in ("limit", "market"):
            raise ValueError(f"unknown order type {self.type!r}")
        if self.type == "limit" and self.price is None:
            raise ValueError("limit orders need a price")
        if not self.close_order and (self.lots is None or self.lots <= 0):
            raise ValueError("order size must be at least one lot")
        if self.post_only and (self.hidden or self.iceberg):
            raise ValueError("post-only cannot be combined with hidden or iceberg")
        if self.iceberg and not self.visible_lots:
            raise ValueError("iceberg orders need a visible size")
        if self.stop and self.stop_price is None:
            raise ValueError("stop orders need a trigger price")

    @property
    def has_tpsl(self) -> bool:
        return self.take_profit is not None or self.stop_loss is not None

    def describe(self) -> str:
        parts = [self.type]
        if self.stop:
            parts.append(f"stop-{self.stop}@{fmt(self.stop_price)}")
        for flag, name in (
            (self.post_only, "post-only"),
            (self.hidden, "hidden"),
            (self.iceberg, f"iceberg(visible {self.visible_lots})"),
            (self.reduce_only, "reduce-only"),
            (self.close_order, "close-position"),
        ):
            if flag:
                parts.append(name)
        if self.time_in_force:
            parts.append(self.time_in_force)
        if self.take_profit is not None:
            parts.append(f"TP {fmt(self.take_profit)}")
        if self.stop_loss is not None:
            parts.append(f"SL {fmt(self.stop_loss)}")
        return " ".join(parts)
