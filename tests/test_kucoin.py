"""KuCoin client tests using a fake HTTP session (no network)."""

import base64
import hashlib
import hmac
import json
from decimal import Decimal

import pytest
import requests

from f3l.exchange import ExchangeError, KuCoin, MissingCredentials
from f3l.models import OrderRequest


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, data=None, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": headers, "data": data})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def ok(data):
    return FakeResponse({"code": "200000", "data": data})


def client(*responses, creds=True):
    kw = dict(api_key="key", secret="secret", passphrase="pass") if creds else {}
    return KuCoin(session=FakeSession(*responses), **kw)


def test_signature_covers_path_query_and_body(monkeypatch):
    monkeypatch.setattr("f3l.exchange.time.time", lambda: 1700000000.0)
    k = client(ok({"orderId": "abc"}))
    req = OrderRequest(side="buy", symbol="XBTUSDTM", lots=3, price=Decimal("60000.1"), leverage=5,
                       client_oid="cid1")
    assert k.place_order(req) == "abc"
    call = k.session.calls[0]
    body = call["data"]
    assert json.loads(body) == {"clientOid": "cid1", "symbol": "XBTUSDTM", "type": "limit", "side": "buy",
                                "size": 3, "leverage": 5, "price": "60000.1"}
    expected = base64.b64encode(hmac.new(b"secret", ("1700000000000POST/api/v1/orders" + body).encode(),
                                         hashlib.sha256).digest()).decode()
    assert call["headers"]["KC-API-SIGN"] == expected
    assert call["headers"]["KC-API-KEY-VERSION"] == "3"
    assert call["headers"]["KC-API-PASSPHRASE"] != "pass"  # passphrase is HMAC-encrypted


def test_query_string_is_signed(monkeypatch):
    monkeypatch.setattr("f3l.exchange.time.time", lambda: 1.0)
    k = client(ok({"cancelledOrderIds": ["a", "b"]}))
    assert k.cancel_all("XBTUSDTM") == ["a", "b"]
    call = k.session.calls[0]
    assert call["url"].endswith("/api/v3/orders?symbol=XBTUSDTM")
    expected = base64.b64encode(hmac.new(b"secret", b"1000DELETE/api/v3/orders?symbol=XBTUSDTM",
                                         hashlib.sha256).digest()).decode()
    assert call["headers"]["KC-API-SIGN"] == expected


def test_public_endpoints_work_without_credentials(monkeypatch):
    for var in ("KuCoinAPI", "KuCoinSecret", "KuCoinPass"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("f3l.exchange.load_dotenv", lambda: None)
    k = client(ok({"bestBidPrice": "1", "bestAskPrice": "2", "price": "1.5"}), creds=False)
    assert k.ticker("XBTUSDTM").ask == Decimal(2)
    assert "KC-API-KEY" not in k.session.calls[0]["headers"]
    with pytest.raises(MissingCredentials):
        k.balance()


def test_api_error_carries_code_and_message():
    k = client(FakeResponse({"code": "300003", "msg": "Balance insufficient"}, status=200))
    with pytest.raises(ExchangeError) as err:
        k.balance()
    assert err.value.code == "300003"
    assert "Balance insufficient" in str(err.value)


def test_non_json_error():
    class Bad(FakeResponse):
        def json(self):
            raise ValueError

    k = client(Bad({}, status=502))
    with pytest.raises(ExchangeError, match="HTTP 502"):
        k.balance()


def test_get_is_retried_but_post_is_not(monkeypatch):
    monkeypatch.setattr("f3l.exchange.time.sleep", lambda s: None)
    k = client(requests.ConnectionError("boom"), ok({"accountEquity": 1}))
    assert k.balance() == {"accountEquity": 1}
    assert len(k.session.calls) == 2

    k = client(requests.Timeout("slow"), ok({"orderId": "x"}))
    with pytest.raises(ExchangeError, match="network error"):
        k.place_order(OrderRequest(side="buy", symbol="X", lots=1, type="market"))
    assert len(k.session.calls) == 1


def test_contract_is_parsed_and_cached():
    k = client(ok({"symbol": "XBTUSDTM", "tickSize": 0.1, "lotSize": 1, "multiplier": 0.001,
                   "isInverse": False, "maxLeverage": 125, "maxOrderQty": 1000000}))
    c = k.contract("XBTUSDTM")
    assert c.tick_size == Decimal("0.1") and c.multiplier == Decimal("0.001") and c.max_leverage == 125
    assert k.contract("XBTUSDTM") is c
    assert len(k.session.calls) == 1


def test_order_body_flags_and_tpsl_mapping():
    body = KuCoin.order_body(OrderRequest(side="sell", symbol="ETHUSDTM", lots=10, price=Decimal("4000"),
                                          iceberg=True, visible_lots=2, reduce_only=True,
                                          take_profit=Decimal(3500), stop_loss=Decimal(4200),
                                          margin_mode="cross", client_oid="c"))
    assert body["iceberg"] is True and body["visibleSize"] == "2"
    assert body["reduceOnly"] is True and body["marginMode"] == "CROSS"
    # for a short, take-profit is below (down) and stop-loss above (up)
    assert body["triggerStopDownPrice"] == "3500"
    assert body["triggerStopUpPrice"] == "4200"
    assert "postOnly" not in body

    stop = KuCoin.order_body(OrderRequest(side=None, symbol="X", type="market", close_order=True,
                                          stop="down", stop_price=Decimal("59000.5"), stop_price_type="MP"))
    assert stop["closeOrder"] is True and stop["stop"] == "down" and stop["stopPrice"] == "59000.5"
    assert "side" not in stop and "size" not in stop


def test_tpsl_orders_use_st_orders_endpoint():
    k = client(ok({"orderId": "x"}))
    k.place_order(OrderRequest(side="buy", symbol="X", lots=1, type="market", stop_loss=Decimal(1)))
    assert "/api/v1/st-orders" in k.session.calls[0]["url"]


def test_batch_orders_map_results_by_client_oid():
    reqs = [OrderRequest(side="buy", symbol="X", lots=1, price=Decimal(i + 1), client_oid=f"c{i}") for i in range(3)]
    k = client(ok([
        {"clientOid": "c2", "orderId": "o2", "code": "200000"},
        {"clientOid": "c0", "orderId": "o0", "code": "200000"},
        {"clientOid": "c1", "orderId": None, "code": "300003", "msg": "Balance insufficient"},
    ]))
    results = k.place_orders(reqs)
    assert results[0] == {"order_id": "o0"}
    assert "Balance insufficient" in results[1]["error"]
    assert results[2] == {"order_id": "o2"}
    assert isinstance(json.loads(k.session.calls[0]["data"]), list)


def test_batch_orders_are_chunked_by_20():
    reqs = [OrderRequest(side="buy", symbol="X", lots=1, price=Decimal(1), client_oid=f"c{i}") for i in range(25)]
    k = client(ok([{"clientOid": f"c{i}", "orderId": f"o{i}"} for i in range(20)]),
               ok([{"clientOid": f"c{i}", "orderId": f"o{i}"} for i in range(20, 25)]))
    assert len(k.place_orders(reqs)) == 25
    assert len(k.session.calls) == 2


def test_order_status_parsing():
    k = client(ok({"id": "o1", "size": 10, "filledSize": 4, "filledValue": "240.5", "isActive": True}))
    st = k.order("o1")
    assert (st.lots, st.filled_lots, st.filled_value, st.is_active, st.remaining) == (10, 4, Decimal("240.5"), True, 6)


def test_paged_open_orders():
    k = client(ok({"currentPage": 1, "totalPage": 2, "items": [{"id": "a"}]}),
               ok({"currentPage": 2, "totalPage": 2, "items": [{"id": "b"}]}))
    assert [o["id"] for o in k.open_orders()] == ["a", "b"]
