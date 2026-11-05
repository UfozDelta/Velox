"""An in-memory exchange and clock for testing without touching KuCoin."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Optional

from f3l.exchange import AbstractExchange, ExchangeError
from f3l.models import BUY, D, Contract, OrderRequest, OrderStatus, Ticker

XBT = Contract("XBTUSDTM", tick_size=Decimal("0.1"), lot_size=1, multiplier=Decimal("0.001"), max_leverage=125)
ETH = Contract("ETHUSDTM", tick_size=Decimal("0.01"), lot_size=1, multiplier=Decimal("0.01"), max_leverage=100)
INV = Contract("XBTUSDM", tick_size=Decimal("0.1"), lot_size=1, multiplier=Decimal("-1"), is_inverse=True)


@dataclass
class FakeOrder:
    id: str
    req: OrderRequest
    filled: int = 0
    value: Decimal = Decimal(0)
    active: bool = True

    def fill(self, lots: int, price: Decimal, contract: Contract) -> None:
        lots = min(lots, self.req.lots - self.filled)
        self.filled += lots
        self.value += contract.lots_to_usd(lots, price)
        if self.filled >= self.req.lots:
            self.active = False


class FakeClock:
    def __init__(self, on_sleep: Optional[Callable[[float], None]] = None):
        self.t = 0.0
        self.on_sleep = on_sleep
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += max(seconds, 0)
        if self.on_sleep:
            self.on_sleep(self.t)


@dataclass
class FakeExchange(AbstractExchange):
    bid: Decimal = Decimal("60000.0")
    ask: Decimal = Decimal("60000.5")
    mark: Decimal = Decimal("60000.2")
    position: int = 0
    contracts: dict = field(default_factory=lambda: {c.symbol: c for c in (XBT, ETH, INV)})
    orders: dict = field(default_factory=dict)
    stops: dict = field(default_factory=dict)
    reject_next: list = field(default_factory=list)  # error messages to raise on next place_order calls
    ioc_fill_ratio: Decimal = Decimal(1)
    kline_data: Callable = None
    name = "fake"

    def __post_init__(self):
        self._ids = itertools.count(1)
        self.placed: list[OrderRequest] = []
        self.cancelled: list[str] = []

    # market data
    def contract(self, symbol):
        if symbol not in self.contracts:
            raise ExchangeError(f"unknown contract {symbol}", "40001")
        return self.contracts[symbol]

    def ticker(self, symbol):
        return Ticker(symbol, self.bid, self.ask, (self.bid + self.ask) / 2)

    def mark_price(self, symbol):
        return {"symbol": symbol, "value": float(self.mark), "indexPrice": float(self.mark)}

    def klines(self, symbol, granularity_min, start_ms, end_ms):
        return self.kline_data(granularity_min, start_ms, end_ms) if self.kline_data else []

    # account
    def balance(self):
        return {"accountEquity": 1000, "availableBalance": 900, "currency": "USDT"}

    def open_orders(self, symbol=None):
        return [self._as_dict(o) for o in self.orders.values()
                if o.active and o.req.stop is None and (symbol is None or o.req.symbol == symbol)]

    def stop_orders(self, symbol=None):
        return [self._as_dict(o) for o in self.stops.values() if symbol is None or o.req.symbol == symbol]

    def positions(self, symbol=None):
        if not self.position:
            return []
        return [{"symbol": symbol or "XBTUSDTM", "currentQty": self.position, "isOpen": True,
                 "avgEntryPrice": 59000, "markPrice": float(self.mark), "unrealisedPnl": 1.5,
                 "unrealisedRoePcnt": 0.05, "realLeverage": 5, "marginMode": "ISOLATED"}]

    def order(self, order_id):
        o = self.orders.get(order_id) or self.stops.get(order_id)
        if o is None:
            raise ExchangeError("order does not exist", "100001")
        return OrderStatus(o.id, o.req.lots or 0, o.filled, o.value, o.active)

    # trading
    def place_order(self, req: OrderRequest) -> str:
        req.validate()
        if self.reject_next:
            raise ExchangeError(self.reject_next.pop(0), "300000")
        oid = f"o{next(self._ids)}"
        self.placed.append(req)
        o = FakeOrder(oid, req)
        c = self.contract(req.symbol)
        if req.stop:
            self.stops[oid] = o
            return oid
        self.orders[oid] = o
        crosses = req.type == "market" or (req.price >= self.ask if req.side == BUY else req.price <= self.bid)
        if req.post_only and crosses:
            o.active = False  # post-only rejected by the matching engine
        elif req.type == "market":
            o.fill(req.lots, self.ask if req.side == BUY else self.bid, c)
            o.active = False
        elif req.time_in_force == "IOC":
            if crosses:
                o.fill(int(req.lots * self.ioc_fill_ratio), req.price, c)
            o.active = False
        return oid

    def place_orders(self, reqs):
        out = []
        for r in reqs:
            try:
                out.append({"order_id": self.place_order(r)})
            except ExchangeError as exc:
                out.append({"error": str(exc)})
        return out

    def cancel_order(self, order_id):
        if order_id in self.stops:
            del self.stops[order_id]
            self.cancelled.append(order_id)
            return
        o = self.orders.get(order_id)
        if o is None or not o.active:
            raise ExchangeError("order already done", "100004")
        o.active = False
        self.cancelled.append(order_id)

    def cancel_all(self, symbol=None):
        ids = [o["id"] for o in self.open_orders(symbol)]
        for i in ids:
            self.cancel_order(i)
        return ids

    def cancel_all_stops(self, symbol=None):
        ids = [o["id"] for o in self.stop_orders(symbol)]
        for i in ids:
            self.cancel_order(i)
        return ids

    # helpers for tests
    def working(self) -> list[FakeOrder]:
        return [o for o in self.orders.values() if o.active]

    def fill_working(self, lots: Optional[int] = None) -> None:
        for o in self.working():
            o.fill(lots if lots is not None else o.req.lots, o.req.price, self.contract(o.req.symbol))

    def trigger_stops(self) -> None:
        """Fire stop orders whose trigger price has been crossed by the mark price."""
        for oid, o in list(self.stops.items()):
            hit = self.mark >= o.req.stop_price if o.req.stop == "up" else self.mark <= o.req.stop_price
            if hit:
                del self.stops[oid]
                if o.req.close_order:
                    self.position = 0

    @staticmethod
    def _as_dict(o: FakeOrder) -> dict:
        return {"id": o.id, "symbol": o.req.symbol, "side": o.req.side, "type": o.req.type,
                "price": str(o.req.price) if o.req.price else None, "size": o.req.lots,
                "filledSize": o.filled, "value": "0", "stop": o.req.stop,
                "stopPrice": str(o.req.stop_price) if o.req.stop_price else None,
                "stopPriceType": o.req.stop_price_type, "closeOrder": o.req.close_order,
                "reduceOnly": o.req.reduce_only, "postOnly": o.req.post_only}
