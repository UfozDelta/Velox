from decimal import Decimal

import pytest

from f3l import ladder
from f3l.config import SafetyError, Settings
from f3l.models import OrderRequest, fmt

from .fakes import ETH, INV, XBT


# ------------------------------------------------------------------ contract
def test_round_price_never_more_aggressive():
    assert XBT.round_price("60000.17", "buy") == Decimal("60000.1")
    assert XBT.round_price("60000.11", "sell") == Decimal("60000.2")
    assert XBT.round_price("60000.15") == Decimal("60000.2")
    assert ETH.round_price("3000.005", "buy") == Decimal("3000.00")


def test_usd_to_lots_linear_and_inverse():
    # one XBTUSDTM lot = 0.001 BTC = $60 at 60k
    assert XBT.usd_to_lots(1000, 60000) == 16
    assert XBT.usd_to_lots(59, 60000) == 0
    assert XBT.lots_to_usd(16, 60000) == Decimal("960.000")
    # inverse: one lot = $1
    assert INV.usd_to_lots(250, 60000) == 250


def test_fmt_has_no_scientific_notation():
    assert fmt(Decimal("1E-5")) == "0.00001"
    assert fmt(Decimal("60000.10")) == "60000.1"
    assert fmt(Decimal("100")) == "100"


@pytest.mark.parametrize("kwargs, message", [
    (dict(side="buy", lots=1, type="limit"), "need a price"),
    (dict(side="buy", lots=0, type="market"), "at least one lot"),
    (dict(side="hold", lots=1, type="market"), "side must be"),
    (dict(side="buy", lots=1, type="limit", price=Decimal(1), post_only=True, hidden=True), "post-only"),
    (dict(side="buy", lots=5, type="limit", price=Decimal(1), iceberg=True), "visible size"),
    (dict(side="buy", lots=1, type="market", stop="up"), "trigger price"),
])
def test_order_request_validation(kwargs, message):
    with pytest.raises(ValueError, match=message):
        OrderRequest(symbol="XBTUSDTM", **kwargs).validate()


def test_close_order_needs_no_size():
    OrderRequest(side=None, symbol="XBTUSDTM", type="market", close_order=True,
                 stop="down", stop_price=Decimal(1)).validate()


# ------------------------------------------------------------------ settings
def test_settings_limits():
    s = Settings(max_leverage=10, max_order_usd=Decimal(5000), confirm_above_usd=Decimal(100))
    s.check(Decimal(4999), 10)
    with pytest.raises(SafetyError, match="leverage 11x"):
        s.check(Decimal(10), 11)
    with pytest.raises(SafetyError, match="contract maximum"):
        s.check(Decimal(10), 5, max_leverage=3)
    with pytest.raises(SafetyError, match="above your limit"):
        s.check(Decimal(5001), 1)
    assert s.needs_confirmation(Decimal(100))
    assert not s.needs_confirmation(Decimal(99))


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("F3L_MAX_LEVERAGE", "3")
    monkeypatch.setenv("F3L_MAX_ORDER_USD", "250")
    s = Settings.from_env()
    assert s.max_leverage == 3 and s.max_order_usd == Decimal(250)
    monkeypatch.setenv("F3L_MAX_LEVERAGE", "lots")
    with pytest.raises(SafetyError):
        Settings.from_env()


# -------------------------------------------------------------------- ladder
def test_weight_shapes():
    assert ladder.weights(3, "even") == [1, 1, 1]
    assert ladder.weights(4, "linear") == [1, 2, 3, 4]
    assert ladder.weights(3, "exp", Decimal(2)) == [1, 2, 4]
    assert ladder.weights(3, "custom", ratios=[Decimal(3), Decimal(1), Decimal(1)]) == [3, 1, 1]
    with pytest.raises(ladder.LadderError, match="has 2 values"):
        ladder.weights(3, "custom", ratios=[Decimal(1), Decimal(2)])


def test_price_spacing():
    assert ladder.prices(Decimal(100), Decimal(90), 3) == [100, 95, 90]
    geo = ladder.prices(Decimal(100), Decimal(81), 3, "geometric")
    assert [round(p, 6) for p in geo] == [100, 90, 81]
    assert ladder.prices(Decimal(100), Decimal(90), 1) == [100]


def test_plan_linear_buy_ladder_puts_size_at_the_bottom():
    rungs = ladder.plan(XBT, "buy", Decimal(60000), Decimal(55000), Decimal(3000), 5, "linear")
    assert [r.price for r in rungs] == [60000, 58750, 57500, 56250, 55000]
    lots = [r.lots for r in rungs]
    assert lots == sorted(lots) and lots[-1] > lots[0]
    assert sum(r.usd for r in rungs) <= 3000


def test_plan_reverse_flips_sizes():
    fwd = ladder.plan(XBT, "buy", Decimal(60000), Decimal(55000), Decimal(3000), 4, "linear")
    rev = ladder.plan(XBT, "buy", Decimal(60000), Decimal(55000), Decimal(3000), 4, "linear", reverse=True)
    assert [r.lots for r in rev][0] > [r.lots for r in fwd][0]


def test_plan_rejects_orders_below_one_lot():
    with pytest.raises(ladder.LadderError, match="smaller than one lot"):
        ladder.plan(XBT, "buy", Decimal(60000), Decimal(55000), Decimal(100), 5)


def test_plan_rejects_duplicate_prices():
    with pytest.raises(ladder.LadderError, match="same price"):
        ladder.plan(XBT, "buy", Decimal("60000.0"), Decimal("60000.2"), Decimal(5000), 5)
