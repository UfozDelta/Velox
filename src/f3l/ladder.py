"""Ladder planning: spread one order across several prices.

Everything here is pure maths, so it can be previewed with --dry-run and
unit tested without an exchange.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Sequence

from .models import D, Contract, fmt

SHAPES = ("even", "linear", "exp", "custom")
SPACINGS = ("linear", "geometric")


class LadderError(ValueError):
    pass


@dataclass(frozen=True)
class Rung:
    price: Decimal
    lots: int
    usd: Decimal


def weights(n: int, shape: str = "even", factor: Decimal = Decimal("1.5"),
            ratios: Optional[Sequence[Decimal]] = None) -> list[Decimal]:
    """Relative size of each rung. The first weight goes to the start price.

    even    1, 1, 1, ...
    linear  1, 2, 3, ...          (the old limit-x3 "3:2:1" sizing)
    exp     1, f, f^2, ...        (factor f, default 1.5)
    custom  the ratios you give, e.g. 1,1,2,3
    """
    if shape == "custom":
        if not ratios:
            raise LadderError("custom shape needs --ratios, e.g. --ratios 1,2,3")
        if len(ratios) != n:
            raise LadderError(f"--ratios has {len(ratios)} values but the ladder has {n} orders")
        result = [D(r) for r in ratios]
    elif shape == "even":
        result = [Decimal(1)] * n
    elif shape == "linear":
        result = [Decimal(i + 1) for i in range(n)]
    elif shape == "exp":
        f = D(factor)
        if f <= 0:
            raise LadderError("--factor must be positive")
        result = [f ** i for i in range(n)]
    else:
        raise LadderError(f"unknown shape {shape!r}; choose from {', '.join(SHAPES)}")
    if any(w < 0 for w in result) or sum(result) <= 0:
        raise LadderError("ratios must be non-negative and not all zero")
    return result


def prices(start: Decimal, end: Decimal, n: int, spacing: str = "linear") -> list[Decimal]:
    """n prices from start to end inclusive.

    linear     equal dollar gaps
    geometric  equal percentage gaps
    """
    start, end = D(start), D(end)
    if n < 1:
        raise LadderError("a ladder needs at least one order")
    if start <= 0 or end <= 0:
        raise LadderError("prices must be positive")
    if n == 1:
        return [start]
    if spacing == "linear":
        step = (end - start) / (n - 1)
        return [start + step * i for i in range(n)]
    if spacing == "geometric":
        ratio = (end / start) ** (Decimal(1) / (n - 1))
        return [start * ratio ** i for i in range(n)]
    raise LadderError(f"unknown spacing {spacing!r}; choose from {', '.join(SPACINGS)}")


def plan(contract: Contract, side: str, start: Decimal, end: Decimal, total_usd: Decimal,
         n: int, shape: str = "even", factor: Decimal = Decimal("1.5"),
         ratios: Optional[Sequence[Decimal]] = None, spacing: str = "linear",
         reverse: bool = False) -> list[Rung]:
    """Turn a ladder description into concrete (price, lots) rungs."""
    w = weights(n, shape, factor, ratios)
    if reverse:
        w = list(reversed(w))
    raw_prices = prices(start, end, n, spacing)
    total_w = sum(w)
    rungs: list[Rung] = []
    for price, weight in zip(raw_prices, w):
        p = contract.round_price(price, side)
        if p <= 0:
            raise LadderError(f"price {fmt(price)} rounds to zero")
        usd = D(total_usd) * weight / total_w
        lots = contract.usd_to_lots(usd, p)
        rungs.append(Rung(price=p, lots=lots, usd=contract.lots_to_usd(lots, p)))

    seen = set()
    for r in rungs:
        if r.price in seen:
            raise LadderError(
                f"two orders land on the same price {fmt(r.price)}: widen the range or use fewer orders"
            )
        seen.add(r.price)
    empty = [r for r, weight in zip(rungs, w) if r.lots == 0 and weight > 0]
    if empty:
        smallest = contract.lot_value(empty[0].price) * contract.lot_size
        raise LadderError(
            f"{len(empty)} order(s) are smaller than one lot (${fmt(smallest.quantize(Decimal('0.01')))} "
            f"at {fmt(empty[0].price)}): increase the size or use fewer orders"
        )
    return [r for r in rungs if r.lots > 0]
