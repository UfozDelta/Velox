"""Order algorithms that run inside the CLI process.

KuCoin has no native chase, TWAP, VWAP or trailing-stop orders, so these
loops drive ordinary orders through the API. They only work while the
command is running: Ctrl+C stops them and cleans up (see each function).

Every loop takes a ``clock`` (for tests) and a ``log`` callback (for output).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Optional, Sequence

from .exchange import AbstractExchange, ExchangeError
from .models import BUY, SELL, D, Contract, OrderRequest, OrderStatus, Ticker, fmt

Log = Callable[[str], None]


class Clock:
    """Wall clock. Tests swap in a fake that advances instantly."""

    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


@dataclass
class ExecResult:
    status: str  # filled | partial | timeout | cancelled | error
    requested_lots: int
    filled_lots: int = 0
    filled_value: Decimal = Decimal(0)
    orders_placed: int = 0
    avg_price: Optional[Decimal] = None
    notes: list[str] = field(default_factory=list)

    @property
    def remaining_lots(self) -> int:
        return self.requested_lots - self.filled_lots


class FillTracker:
    """Keeps the latest fill figures per order, so re-reads never double count."""

    def __init__(self) -> None:
        self._by_order: dict[str, tuple[int, Decimal]] = {}

    def update(self, status: OrderStatus) -> None:
        self._by_order[status.order_id] = (status.filled_lots, status.filled_value)

    @property
    def lots(self) -> int:
        return sum(lots for lots, _ in self._by_order.values())

    @property
    def value(self) -> Decimal:
        return sum((value for _, value in self._by_order.values()), Decimal(0))


def avg_fill_price(contract: Contract, lots: int, value: Decimal) -> Optional[Decimal]:
    if lots <= 0 or value <= 0:
        return None
    if contract.is_inverse:
        return None  # filled value is reported in coin terms; skip
    return (value / (abs(contract.multiplier) * lots)).quantize(contract.tick_size)


def _result(status: str, lots: int, fills: FillTracker, placed: int, contract: Contract) -> ExecResult:
    return ExecResult(
        status=status,
        requested_lots=lots,
        filled_lots=fills.lots,
        filled_value=fills.value,
        orders_placed=placed,
        avg_price=avg_fill_price(contract, fills.lots, fills.value),
    )


def _cancel_and_record(ex: AbstractExchange, order_id: str, fills: FillTracker, log: Log) -> Optional[OrderStatus]:
    """Cancel an order, then record its final fills. Returns the final status."""
    try:
        ex.cancel_order(order_id)
    except ExchangeError as exc:
        # Usually "order already filled/cancelled"; the status read below settles it.
        log(f"  cancel {order_id}: {exc}")
    try:
        status = ex.order(order_id)
    except ExchangeError as exc:
        log(f"  could not read final status of {order_id}: {exc}")
        return None
    fills.update(status)
    return status


def _wait_until_done(ex: AbstractExchange, order_id: str, clock: Clock, wait: float, poll: float = 0.5) -> OrderStatus:
    deadline = clock.now() + wait
    status = ex.order(order_id)
    while status.is_active and clock.now() < deadline:
        clock.sleep(poll)
        status = ex.order(order_id)
    return status


# --------------------------------------------------------------------- chase
def chase_price(contract: Contract, side: str, ticker: Ticker, offset_ticks: int = 0,
                limit_price: Optional[Decimal] = None) -> Decimal:
    """Best passive price: join the bid (buy) or ask (sell), optionally
    improved by ``offset_ticks`` but never crossing the spread or the cap."""
    tick = contract.tick_size
    if side == BUY:
        price = ticker.bid + tick * offset_ticks
        if price >= ticker.ask:
            price = ticker.ask - tick
        if limit_price is not None:
            price = min(price, D(limit_price))
    else:
        price = ticker.ask - tick * offset_ticks
        if price <= ticker.bid:
            price = ticker.bid + tick
        if limit_price is not None:
            price = max(price, D(limit_price))
    return contract.round_price(max(price, tick), side)


def chase(ex: AbstractExchange, contract: Contract, side: str, lots: int, *,
          leverage: Optional[int] = None, limit_price: Optional[Decimal] = None,
          timeout: Optional[float] = None, interval: float = 1.0, offset_ticks: int = 0,
          reduce_only: bool = False, margin_mode: Optional[str] = None,
          on_timeout: str = "cancel", max_errors: int = 5,
          clock: Optional[Clock] = None, log: Log = print) -> ExecResult:
    """Keep a post-only limit order at the best bid/ask until it fills.

    ``limit_price`` caps how far it will chase. On timeout the working order
    is cancelled, and with ``on_timeout="market"`` the rest is sent at market.
    Ctrl+C cancels the working order.
    """
    clock = clock or Clock()
    symbol = contract.symbol
    fills = FillTracker()
    working_id: Optional[str] = None
    working_price: Optional[Decimal] = None
    placed = 0
    errors = 0
    status = "filled"
    started = clock.now()
    try:
        while fills.lots < lots:
            if timeout is not None and clock.now() - started >= timeout:
                status = "timeout"
                break
            target = chase_price(contract, side, ex.ticker(symbol), offset_ticks, limit_price)
            if working_id:
                st = ex.order(working_id)
                fills.update(st)
                if fills.lots >= lots and not st.is_active:
                    working_id = None
                    break
                if not st.is_active:
                    # Filled part-way then done, or a post-only order that would have crossed.
                    working_id = None
                elif target != working_price:
                    log(f"  price moved {fmt(working_price)} -> {fmt(target)}, re-pricing")
                    _cancel_and_record(ex, working_id, fills, log)
                    working_id = None
                    if fills.lots >= lots:
                        break
            if working_id is None:
                remaining = lots - fills.lots
                req = OrderRequest(side=side, symbol=symbol, lots=remaining, type="limit",
                                   price=target, leverage=leverage, post_only=True,
                                   reduce_only=reduce_only, margin_mode=margin_mode)
                try:
                    working_id = ex.place_order(req)
                    working_price = target
                    placed += 1
                    errors = 0
                    log(f"  {side} {remaining} lots @ {fmt(target)}  (filled {fills.lots}/{lots})")
                except ExchangeError as exc:
                    errors += 1
                    log(f"  order rejected: {exc}")
                    if errors >= max_errors:
                        status = "error"
                        break
            clock.sleep(interval)
    except KeyboardInterrupt:
        status = "cancelled"
        log("  interrupted, cancelling working order...")
    finally:
        if working_id:
            _cancel_and_record(ex, working_id, fills, log)

    if fills.lots >= lots:
        status = "filled"
    elif status == "timeout" and on_timeout == "market":
        remaining = lots - fills.lots
        log(f"  timeout: sending remaining {remaining} lots at market")
        req = OrderRequest(side=side, symbol=symbol, lots=remaining, type="market",
                           leverage=leverage, reduce_only=reduce_only, margin_mode=margin_mode)
        try:
            oid = ex.place_order(req)
            placed += 1
            fills.update(_wait_until_done(ex, oid, clock, wait=10))
            status = "filled" if fills.lots >= lots else "partial"
        except ExchangeError as exc:
            log(f"  market order failed: {exc}")
    return _result(status, lots, fills, placed, contract)


# ------------------------------------------------------------- TWAP / VWAP
@dataclass(frozen=True)
class Slice:
    offset: float  # seconds after start
    lots: int


def split_lots(total: int, weights: Sequence[Decimal], lot_size: int = 1) -> list[int]:
    """Split ``total`` lots by weight, in whole multiples of ``lot_size``,
    using largest-remainder so the parts always add up exactly."""
    units = total // lot_size
    weights = [D(w) for w in weights]
    total_w = sum(weights)
    if total_w <= 0:
        weights = [Decimal(1)] * len(weights)
        total_w = Decimal(len(weights))
    exact = [Decimal(units) * w / total_w for w in weights]
    parts = [int(x) for x in exact]
    leftovers = sorted(range(len(exact)), key=lambda i: exact[i] - parts[i], reverse=True)
    for i in leftovers[: units - sum(parts)]:
        parts[i] += 1
    return [p * lot_size for p in parts]


def make_schedule(total_lots: int, duration: float, weights: Sequence[Decimal], lot_size: int = 1) -> list[Slice]:
    """One slice per weight, evenly spaced over ``duration`` (first slice now)."""
    n = len(weights)
    if n < 1:
        raise ValueError("need at least one slice")
    interval = duration / n
    sizes = split_lots(total_lots, weights, lot_size)
    return [Slice(offset=i * interval, lots=size) for i, size in enumerate(sizes)]


KLINE_GRANULARITIES = (1, 5, 15, 30, 60, 120, 240, 480, 720, 1440)
KLINE_LIMIT = 500


def volume_profile(ex: AbstractExchange, symbol: str, start_epoch: float, duration: float,
                   slices: int, lookback_days: int = 7) -> list[Decimal]:
    """Typical traded volume in each slice of the upcoming window, averaged
    over the same time of day on the previous ``lookback_days`` days.

    Falls back to equal weights (plain TWAP) if there is no history.
    """
    slice_len = duration / slices
    gran = max([g for g in KLINE_GRANULARITIES if g * 60 <= slice_len] or [1])
    step_ms = gran * 60 * 1000
    buckets = [Decimal(0)] * slices
    for day in range(1, lookback_days + 1):
        window_start = int((start_epoch - day * 86400) * 1000)
        window_end = window_start + int(duration * 1000)
        cursor = window_start
        while cursor < window_end:
            chunk_end = min(window_end, cursor + step_ms * KLINE_LIMIT)
            for candle in ex.klines(symbol, gran, cursor, chunk_end):
                if len(candle) < 6:
                    continue
                idx = int((int(candle[0]) - window_start) / 1000 / slice_len)
                if 0 <= idx < slices:
                    buckets[idx] += D(candle[5])
            cursor = chunk_end
    if sum(buckets) <= 0:
        return [Decimal(1)] * slices
    return buckets


def run_schedule(ex: AbstractExchange, contract: Contract, side: str, schedule: Sequence[Slice], *,
                 leverage: Optional[int] = None, limit_price: Optional[Decimal] = None,
                 reduce_only: bool = False, margin_mode: Optional[str] = None,
                 fill_wait: float = 5.0, clock: Optional[Clock] = None, log: Log = print) -> ExecResult:
    """Send each slice at its time. Slices are market orders, or IOC limit
    orders at ``limit_price`` when a cap is given (so nothing fills worse than
    the cap). Anything a slice fails to fill rolls into the next slice.
    Ctrl+C stops before the next slice."""
    clock = clock or Clock()
    total = sum(s.lots for s in schedule)
    fills = FillTracker()
    placed = 0
    carry = 0
    status = "filled"
    started = clock.now()
    try:
        for n, sl in enumerate(schedule, 1):
            wait = started + sl.offset - clock.now()
            if wait > 0:
                clock.sleep(wait)
            qty = sl.lots + carry
            if qty <= 0:
                continue
            if limit_price is not None:
                req = OrderRequest(side=side, symbol=contract.symbol, lots=qty, type="limit",
                                   price=contract.round_price(limit_price, side), time_in_force="IOC",
                                   leverage=leverage, reduce_only=reduce_only, margin_mode=margin_mode)
            else:
                req = OrderRequest(side=side, symbol=contract.symbol, lots=qty, type="market",
                                   leverage=leverage, reduce_only=reduce_only, margin_mode=margin_mode)
            try:
                oid = ex.place_order(req)
                placed += 1
                st = _wait_until_done(ex, oid, clock, fill_wait)
                if st.is_active:  # shouldn't happen for IOC/market, but never leave it behind
                    st = _cancel_and_record(ex, oid, fills, log) or st
                fills.update(st)
                carry = qty - st.filled_lots
            except ExchangeError as exc:
                log(f"  slice {n} rejected: {exc}")
                carry = qty
            log(f"  slice {n}/{len(schedule)}: sent {qty}, filled {fills.lots}/{total} lots")
    except KeyboardInterrupt:
        status = "cancelled"
        log("  interrupted, no more slices will be sent")
    if status != "cancelled":
        status = "filled" if fills.lots >= total else "partial"
    return _result(status, total, fills, placed, contract)


# ------------------------------------------------------------ trailing stop
@dataclass
class TrailResult:
    status: str  # triggered | closed | stopped | error
    stop_order_id: Optional[str]
    stop_price: Optional[Decimal]
    best_price: Optional[Decimal]


def trail_level(contract: Contract, long: bool, extreme: Decimal, percent: Decimal) -> Decimal:
    """Stop price ``percent`` away from the best price seen."""
    pct = D(percent) / 100
    if long:
        return contract.round_price(extreme * (1 - pct), BUY)  # round down: never above the trail
    return contract.round_price(extreme * (1 + pct), SELL)  # round up


def trailing_stop(ex: AbstractExchange, contract: Contract, *, percent: Decimal,
                  activation: Optional[Decimal] = None, interval: float = 2.0,
                  trigger: str = "MP", cancel_on_exit: bool = False,
                  max_loops: Optional[int] = None, clock: Optional[Clock] = None,
                  log: Log = print) -> TrailResult:
    """Trail a stop behind the open position on ``contract``.

    The stop is a real stop-market order on KuCoin (close-position), moved up
    (long) or down (short) as the price improves. A new stop is placed before
    the old one is cancelled, so the position is never left unprotected. If
    this command exits, the last stop stays on the exchange unless
    ``cancel_on_exit`` is set.
    """
    clock = clock or Clock()
    symbol = contract.symbol
    qty = ex.position_qty(symbol)
    if qty == 0:
        raise ValueError(f"no open position on {symbol}")
    long = qty > 0

    def current_price() -> Decimal:
        if trigger == "MP":
            return D(ex.mark_price(symbol)["value"])
        return ex.ticker(symbol).last

    price = current_price()
    extreme = price
    active = activation is None or (price >= activation if long else price <= activation)
    if not active:
        log(f"  waiting for price to reach {fmt(activation)} before trailing (now {fmt(price)})")
    stop_id: Optional[str] = None
    stop_level: Optional[Decimal] = None
    status = "stopped"
    loops = 0
    try:
        while max_loops is None or loops < max_loops:
            loops += 1
            if stop_id:
                live_ids = {o.get("id") for o in ex.stop_orders(symbol)}
                if stop_id not in live_ids:
                    if ex.position_qty(symbol) == 0:
                        status = "triggered"
                        log(f"  stop triggered at about {fmt(stop_level)}; position closed")
                        return TrailResult(status, stop_id, stop_level, extreme)
                    log("  stop order disappeared (cancelled elsewhere?); placing a new one")
                    stop_id, stop_level = None, None
            if ex.position_qty(symbol) == 0:
                status = "closed"
                log("  position closed")
                if stop_id:
                    _cancel_quietly(ex, stop_id, log)
                    stop_id = None
                return TrailResult(status, None, stop_level, extreme)

            price = current_price()
            if not active and (price >= activation if long else price <= activation):
                active = True
                extreme = price
                log(f"  activation price reached at {fmt(price)}, trailing started")
            if active:
                extreme = max(extreme, price) if long else min(extreme, price)
                level = trail_level(contract, long, extreme, percent)
                better = stop_level is None or (level > stop_level if long else level < stop_level)
                if better:
                    req = OrderRequest(side=None, symbol=symbol, type="market", close_order=True,
                                       stop="down" if long else "up", stop_price=level,
                                       stop_price_type=trigger)
                    try:
                        new_id = ex.place_order(req)
                    except ExchangeError as exc:
                        log(f"  could not move stop to {fmt(level)}: {exc}")
                    else:
                        if stop_id:
                            _cancel_quietly(ex, stop_id, log)
                        log(f"  stop {'placed' if stop_id is None else 'moved'} at {fmt(level)} (best {fmt(extreme)})")
                        stop_id, stop_level = new_id, level
            clock.sleep(interval)
    except KeyboardInterrupt:
        log("  interrupted")
    if cancel_on_exit and stop_id:
        _cancel_quietly(ex, stop_id, log)
        log("  stop cancelled (--cancel-on-exit)")
        stop_id = None
    elif stop_id:
        log(f"  stop {stop_id} at {fmt(stop_level)} stays on KuCoin (it will no longer trail)")
    return TrailResult(status, stop_id, stop_level, extreme)


def _cancel_quietly(ex: AbstractExchange, order_id: str, log: Log) -> None:
    try:
        ex.cancel_order(order_id)
    except ExchangeError as exc:
        log(f"  cancel {order_id}: {exc}")
