"""Exchange clients.

``AbstractExchange`` is the interface the CLI and the order algorithms use.
``KuCoin`` implements it for KuCoin Futures (USDT-margined and inverse).
Every method returns plain data; formatting lives in the CLI.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from abc import ABC, abstractmethod
from typing import Any, Iterable, Optional
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

from .models import D, Contract, OrderRequest, OrderStatus, Ticker, fmt


class ExchangeError(Exception):
    """The exchange rejected a request or could not be reached."""

    def __init__(self, message: str, code: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}" if self.code else self.message


class MissingCredentials(ExchangeError):
    pass


class AbstractExchange(ABC):
    """What every exchange must provide."""

    name = "abstract"

    # --- market data (no credentials needed) ---
    @abstractmethod
    def contract(self, symbol: str) -> Contract: ...

    @abstractmethod
    def ticker(self, symbol: str) -> Ticker: ...

    @abstractmethod
    def mark_price(self, symbol: str) -> dict: ...

    @abstractmethod
    def klines(self, symbol: str, granularity_min: int, start_ms: int, end_ms: int) -> list[list]: ...

    # --- account ---
    @abstractmethod
    def balance(self) -> dict: ...

    @abstractmethod
    def open_orders(self, symbol: Optional[str] = None) -> list[dict]: ...

    @abstractmethod
    def stop_orders(self, symbol: Optional[str] = None) -> list[dict]: ...

    @abstractmethod
    def positions(self, symbol: Optional[str] = None) -> list[dict]: ...

    @abstractmethod
    def order(self, order_id: str) -> OrderStatus: ...

    # --- trading ---
    @abstractmethod
    def place_order(self, req: OrderRequest) -> str:
        """Place one order and return its exchange order id."""

    @abstractmethod
    def place_orders(self, reqs: list[OrderRequest]) -> list[dict]:
        """Place several orders. Returns one dict per order with
        ``order_id`` on success or ``error`` on failure, in input order."""

    @abstractmethod
    def cancel_order(self, order_id: str) -> None: ...

    @abstractmethod
    def cancel_all(self, symbol: Optional[str] = None) -> list[str]:
        """Cancel open limit orders (all symbols when symbol is None)."""

    @abstractmethod
    def cancel_all_stops(self, symbol: Optional[str] = None) -> list[str]:
        """Cancel untriggered stop orders (all symbols when symbol is None)."""

    def position_qty(self, symbol: str) -> int:
        """Signed position size in lots (positive long, negative short)."""
        qty = 0
        for pos in self.positions(symbol):
            if pos.get("symbol") == symbol and pos.get("isOpen", True):
                qty += int(pos.get("currentQty") or 0)
        return qty


class KuCoin(AbstractExchange):
    """KuCoin Futures REST client."""

    name = "kucoin"
    BASE_URL = "https://api-futures.kucoin.com"
    BATCH_LIMIT = 20
    GET_RETRIES = 2

    def __init__(
        self,
        api_key: Optional[str] = None,
        secret: Optional[str] = None,
        passphrase: Optional[str] = None,
        base_url: str = BASE_URL,
        timeout: float = 10.0,
        session: Optional[requests.Session] = None,
    ):
        load_dotenv()
        self.api_key = api_key or os.getenv("KuCoinAPI")
        self.secret = secret or os.getenv("KuCoinSecret")
        self.passphrase = passphrase or os.getenv("KuCoinPass")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self._contracts: dict[str, Contract] = {}

    # ------------------------------------------------------------------ http
    def _sign(self, text: str) -> str:
        digest = hmac.new(self.secret.encode(), text.encode(), hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    def _auth_headers(self, method: str, path: str, body: str) -> dict:
        if not all([self.api_key, self.secret, self.passphrase]):
            raise MissingCredentials(
                "KuCoin API credentials missing: set KuCoinAPI, KuCoinSecret and "
                "KuCoinPass in your environment or a .env file"
            )
        timestamp = str(int(time.time() * 1000))
        return {
            "KC-API-KEY": self.api_key,
            "KC-API-SIGN": self._sign(timestamp + method + path + body),
            "KC-API-TIMESTAMP": timestamp,
            "KC-API-PASSPHRASE": self._sign(self.passphrase),
            "KC-API-KEY-VERSION": "3",
        }

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        body: Any = None,
        auth: bool = True,
    ) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if params:
            path = f"{path}?{urlencode(params)}"
        body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""
        # Only reads are retried: retrying a POST/DELETE could duplicate an action.
        attempts = 1 + (self.GET_RETRIES if method == "GET" else 0)
        last_error: Optional[Exception] = None
        for attempt in range(attempts):
            headers = {"Content-Type": "application/json"}
            if auth:
                headers.update(self._auth_headers(method, path, body_str))
            try:
                resp = self.session.request(
                    method,
                    self.base_url + path,
                    headers=headers,
                    data=body_str or None,
                    timeout=self.timeout,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(0.5 * (attempt + 1))
                continue
            return self._parse(resp)
        raise ExchangeError(f"network error talking to KuCoin: {last_error}")

    @staticmethod
    def _parse(resp: requests.Response) -> Any:
        try:
            payload = resp.json()
        except ValueError:
            raise ExchangeError(
                f"HTTP {resp.status_code} from KuCoin: {resp.text[:200]}"
            ) from None
        code = str(payload.get("code", ""))
        if code != "200000":
            raise ExchangeError(payload.get("msg") or f"HTTP {resp.status_code}", code)
        return payload.get("data")

    # ----------------------------------------------------------- market data
    def contract(self, symbol: str) -> Contract:
        if symbol not in self._contracts:
            data = self._request("GET", f"/api/v1/contracts/{symbol}", auth=False)
            if not data:
                raise ExchangeError(f"unknown contract {symbol}")
            self._contracts[symbol] = Contract(
                symbol=data["symbol"],
                tick_size=D(data["tickSize"]),
                lot_size=int(data.get("lotSize") or 1),
                multiplier=D(data["multiplier"]),
                is_inverse=bool(data.get("isInverse")),
                max_leverage=int(data["maxLeverage"]) if data.get("maxLeverage") else None,
                max_order_lots=int(data["maxOrderQty"]) if data.get("maxOrderQty") else None,
            )
        return self._contracts[symbol]

    def ticker(self, symbol: str) -> Ticker:
        data = self._request("GET", "/api/v1/ticker", {"symbol": symbol}, auth=False)
        return Ticker(
            symbol=symbol,
            bid=D(data["bestBidPrice"]),
            ask=D(data["bestAskPrice"]),
            last=D(data["price"]),
        )

    def mark_price(self, symbol: str) -> dict:
        return self._request("GET", f"/api/v1/mark-price/{symbol}/current", auth=False)

    def klines(self, symbol: str, granularity_min: int, start_ms: int, end_ms: int) -> list[list]:
        params = {"symbol": symbol, "granularity": granularity_min, "from": start_ms, "to": end_ms}
        return self._request("GET", "/api/v1/kline/query", params, auth=False) or []

    # --------------------------------------------------------------- account
    def balance(self) -> dict:
        return self._request("GET", "/api/v1/account-overview", {"currency": "USDT"})

    def _paged_items(self, path: str, params: dict) -> list[dict]:
        items: list[dict] = []
        page = 1
        while True:
            data = self._request("GET", path, {**params, "currentPage": page, "pageSize": 100})
            if isinstance(data, list):  # some endpoints return a bare list
                return data
            items.extend(data.get("items") or [])
            if page >= int(data.get("totalPage") or 1):
                return items
            page += 1

    def open_orders(self, symbol: Optional[str] = None) -> list[dict]:
        return self._paged_items("/api/v1/orders", {"status": "active", "symbol": symbol})

    def stop_orders(self, symbol: Optional[str] = None) -> list[dict]:
        return self._paged_items("/api/v1/stopOrders", {"symbol": symbol})

    def positions(self, symbol: Optional[str] = None) -> list[dict]:
        if symbol:
            data = self._request("GET", "/api/v1/position", {"symbol": symbol})
            return [data] if data else []
        return self._request("GET", "/api/v1/positions") or []

    def order(self, order_id: str) -> OrderStatus:
        data = self._request("GET", f"/api/v1/orders/{order_id}")
        return OrderStatus(
            order_id=data.get("id", order_id),
            lots=int(data.get("size") or 0),
            filled_lots=int(data.get("filledSize") or data.get("dealSize") or 0),
            filled_value=D(data.get("filledValue") or data.get("dealValue") or 0),
            is_active=bool(data.get("isActive")),
        )

    # --------------------------------------------------------------- trading
    @staticmethod
    def order_body(req: OrderRequest) -> dict:
        """Translate an OrderRequest into KuCoin's JSON body."""
        req.validate()
        body: dict[str, Any] = {
            "clientOid": req.client_oid,
            "symbol": req.symbol,
            "type": req.type,
        }
        if req.side:
            body["side"] = req.side
        if req.lots is not None:
            body["size"] = int(req.lots)
        if req.leverage is not None:
            body["leverage"] = int(req.leverage)
        if req.type == "limit":
            body["price"] = fmt(req.price)
        if req.margin_mode:
            body["marginMode"] = req.margin_mode.upper()
        if req.time_in_force:
            body["timeInForce"] = req.time_in_force
        for attr, key in (
            ("post_only", "postOnly"),
            ("hidden", "hidden"),
            ("iceberg", "iceberg"),
            ("reduce_only", "reduceOnly"),
            ("close_order", "closeOrder"),
        ):
            if getattr(req, attr):
                body[key] = True
        if req.iceberg:
            body["visibleSize"] = str(req.visible_lots)
        if req.stop:
            body["stop"] = req.stop
            body["stopPrice"] = fmt(req.stop_price)
            body["stopPriceType"] = req.stop_price_type
        if req.has_tpsl:
            body["stopPriceType"] = req.stop_price_type
            if req.take_profit is not None:
                body["triggerStopUpPrice" if req.side == "buy" else "triggerStopDownPrice"] = fmt(req.take_profit)
            if req.stop_loss is not None:
                body["triggerStopDownPrice" if req.side == "buy" else "triggerStopUpPrice"] = fmt(req.stop_loss)
        return body

    def place_order(self, req: OrderRequest) -> str:
        body = self.order_body(req)
        # Orders with attached take-profit / stop-loss use the st-orders endpoint.
        path = "/api/v1/st-orders" if req.has_tpsl else "/api/v1/orders"
        data = self._request("POST", path, body=body)
        return data["orderId"]

    def place_orders(self, reqs: list[OrderRequest]) -> list[dict]:
        results: list[dict] = []
        for chunk in _chunks(reqs, self.BATCH_LIMIT):
            if any(r.has_tpsl for r in chunk):
                raise ValueError("batch orders cannot carry take-profit/stop-loss")
            bodies = [self.order_body(r) for r in chunk]
            try:
                data = self._request("POST", "/api/v1/orders/multi", body=bodies) or []
            except ExchangeError as exc:
                results.extend({"error": str(exc)} for _ in chunk)
                continue
            by_oid = {item.get("clientOid"): item for item in data}
            for req in chunk:
                item = by_oid.get(req.client_oid, {})
                if item.get("orderId") and str(item.get("code", "200000")) == "200000":
                    results.append({"order_id": item["orderId"]})
                else:
                    results.append({"error": f"[{item.get('code')}] {item.get('msg') or 'rejected'}"})
        return results

    def cancel_order(self, order_id: str) -> None:
        self._request("DELETE", f"/api/v1/orders/{order_id}")

    def cancel_all(self, symbol: Optional[str] = None) -> list[str]:
        if symbol:
            data = self._request("DELETE", "/api/v3/orders", {"symbol": symbol})
        else:
            data = self._request("DELETE", "/api/v1/orders")
        return list((data or {}).get("cancelledOrderIds") or [])

    def cancel_all_stops(self, symbol: Optional[str] = None) -> list[str]:
        data = self._request("DELETE", "/api/v1/stopOrders", {"symbol": symbol})
        return list((data or {}).get("cancelledOrderIds") or [])


def _chunks(items: list, size: int) -> Iterable[list]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


EXCHANGES = {"kucoin": KuCoin}
