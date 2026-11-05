from decimal import Decimal

import pytest

from f3l import algos

from .fakes import XBT, FakeClock, FakeExchange

quiet = lambda msg: None  # noqa: E731


# --------------------------------------------------------------------- chase
def test_chase_price_joins_bid_and_respects_cap():
    ex = FakeExchange()
    t = ex.ticker("XBTUSDTM")
    assert algos.chase_price(XBT, "buy", t) == Decimal("60000.0")
    assert algos.chase_price(XBT, "sell", t) == Decimal("60000.5")
    # offset that would cross the spread stays one tick inside
    assert algos.chase_price(XBT, "buy", t, offset_ticks=50) == Decimal("60000.4")
    assert algos.chase_price(XBT, "buy", t, limit_price=Decimal(59000)) == Decimal("59000.0")


def test_chase_fills_and_reprices_when_market_moves():
    ex = FakeExchange()
    steps = iter([
        lambda: ex.fill_working(4),                                  # partial fill at 60000
        lambda: (setattr(ex, "bid", Decimal("60010")), setattr(ex, "ask", Decimal("60010.5"))),
        lambda: ex.fill_working(),                                   # rest fills at new price
    ])
    clock = FakeClock(on_sleep=lambda t: next(steps, lambda: None)())
    res = algos.chase(ex, XBT, "buy", 10, clock=clock, log=quiet)
    assert res.status == "filled" and res.filled_lots == 10
    prices = [r.price for r in ex.placed]
    assert prices == [Decimal("60000.0"), Decimal("60010.0")]
    assert ex.placed[1].lots == 6  # only the remainder is re-sent
    assert all(r.post_only for r in ex.placed)
    assert not ex.working()


def test_chase_stops_at_cap():
    ex = FakeExchange()
    clock = FakeClock(on_sleep=lambda t: (setattr(ex, "bid", ex.bid + 100), setattr(ex, "ask", ex.ask + 100)))
    res = algos.chase(ex, XBT, "buy", 5, limit_price=Decimal("60200"), timeout=10, clock=clock, log=quiet)
    assert res.status == "timeout" and res.filled_lots == 0
    assert max(r.price for r in ex.placed) == Decimal("60200.0")
    assert not ex.working()  # cancelled on the way out


def test_chase_timeout_then_market():
    ex = FakeExchange()
    res = algos.chase(ex, XBT, "sell", 5, timeout=3, on_timeout="market", clock=FakeClock(), log=quiet)
    assert res.status == "filled" and res.filled_lots == 5
    assert ex.placed[-1].type == "market" and ex.placed[-1].lots == 5


def test_chase_survives_post_only_rejection():
    ex = FakeExchange()
    # first order would cross (ask dropped to bid level) so the engine kills it
    ex.ask = ex.bid
    calls = iter([lambda: setattr(ex, "ask", Decimal("60000.5")), lambda: ex.fill_working()])
    clock = FakeClock(on_sleep=lambda t: next(calls, lambda: None)())
    res = algos.chase(ex, XBT, "buy", 3, clock=clock, log=quiet)
    assert res.status == "filled"
    assert len(ex.placed) == 2


def test_chase_gives_up_after_repeated_errors():
    ex = FakeExchange(reject_next=["Balance insufficient"] * 5)
    res = algos.chase(ex, XBT, "buy", 3, clock=FakeClock(), log=quiet)
    assert res.status == "error" and res.filled_lots == 0


def test_chase_ctrl_c_cancels_working_order():
    ex = FakeExchange()

    def interrupt(t):
        raise KeyboardInterrupt

    res = algos.chase(ex, XBT, "buy", 3, clock=FakeClock(on_sleep=interrupt), log=quiet)
    assert res.status == "cancelled"
    assert not ex.working() and ex.cancelled


# ------------------------------------------------------------- twap / vwap
def test_split_lots_is_exact():
    assert algos.split_lots(10, [1, 1, 1]) == [4, 3, 3]
    assert sum(algos.split_lots(101, [Decimal("0.3"), 5, 2, 0])) == 101
    assert algos.split_lots(10, [1, 1], lot_size=5) == [5, 5]
    assert algos.split_lots(7, [0, 0]) == [4, 3]  # zero weights fall back to even


def test_schedule_spacing():
    sched = algos.make_schedule(12, 600, [1, 1, 1, 1])
    assert [s.offset for s in sched] == [0, 150, 300, 450]
    assert sum(s.lots for s in sched) == 12


def test_run_schedule_market_slices():
    ex = FakeExchange()
    clock = FakeClock()
    sched = algos.make_schedule(9, 90, [1, 1, 1])
    res = algos.run_schedule(ex, XBT, "buy", sched, clock=clock, log=quiet)
    assert res.status == "filled" and res.filled_lots == 9 and res.orders_placed == 3
    assert clock.t >= 60
    assert all(r.type == "market" for r in ex.placed)


def test_run_schedule_ioc_cap_rolls_unfilled_into_next_slice():
    ex = FakeExchange(ioc_fill_ratio=Decimal("0.5"))
    sched = algos.make_schedule(8, 60, [1, 1])
    res = algos.run_schedule(ex, XBT, "buy", sched, limit_price=Decimal(61000), clock=FakeClock(), log=quiet)
    assert [r.lots for r in ex.placed] == [4, 6]  # 2 unfilled lots carried over
    assert all(r.time_in_force == "IOC" and r.price == Decimal("61000.0") for r in ex.placed)
    assert res.status == "partial" and res.filled_lots == 5


def test_volume_profile_weights_busy_slices():
    start = 1_700_000_000
    ex = FakeExchange()

    def klines(gran, start_ms, end_ms):
        # 1-minute candles: first 5 minutes of the window heavy, next 5 light
        return [[t, 0, 0, 0, 0, 10 if (t - start_ms) < 300_000 else 1] for t in range(start_ms, end_ms, 60_000)]

    ex.kline_data = klines
    w = algos.volume_profile(ex, "XBTUSDTM", start, 600, 2, lookback_days=3)
    assert w[0] == 10 * w[1]


def test_volume_profile_falls_back_to_even():
    w = algos.volume_profile(FakeExchange(), "XBTUSDTM", 1_700_000_000, 600, 4, lookback_days=1)
    assert w == [1, 1, 1, 1]


# ------------------------------------------------------------ trailing stop
def test_trail_level_rounding():
    assert algos.trail_level(XBT, True, Decimal(60000), Decimal(1)) == Decimal("59400.0")
    assert algos.trail_level(XBT, False, Decimal(60000), Decimal(1)) == Decimal("60600.0")


def test_trailing_stop_long_moves_up_then_triggers():
    ex = FakeExchange(position=5, mark=Decimal(60000))
    path = iter([60500, 61000, 60800, 60300])

    def tick(t):
        ex.mark = Decimal(next(path, 60300))
        ex.trigger_stops()

    res = algos.trailing_stop(ex, XBT, percent=Decimal(1), clock=FakeClock(on_sleep=tick), log=quiet)
    assert res.status == "triggered"
    levels = [r.stop_price for r in ex.placed]
    assert levels == [Decimal("59400.0"), Decimal("59895.0"), Decimal("60390.0")]
    assert all(r.close_order and r.stop == "down" for r in ex.placed)
    assert ex.position == 0


def test_trailing_stop_short_and_activation():
    ex = FakeExchange(position=-3, mark=Decimal(60000))
    path = iter([59800, 59000, 58500])
    res = algos.trailing_stop(
        ex, XBT, percent=Decimal(2), activation=Decimal(59000), max_loops=4,
        clock=FakeClock(on_sleep=lambda t: setattr(ex, "mark", Decimal(next(path, 58500)))), log=quiet)
    # nothing placed until price reached 59000, then the stop follows it down
    assert [r.stop_price for r in ex.placed] == [Decimal("60180.0"), Decimal("59670.0")]
    assert all(r.stop == "up" for r in ex.placed)
    assert res.stop_order_id in ex.stops  # left in place on exit
    assert len(ex.stops) == 1  # the old stop was cancelled after the new one went in


def test_trailing_stop_cancel_on_exit():
    ex = FakeExchange(position=1, mark=Decimal(60000))
    res = algos.trailing_stop(ex, XBT, percent=Decimal(1), max_loops=2, cancel_on_exit=True,
                              clock=FakeClock(), log=quiet)
    assert res.stop_order_id is None and not ex.stops


def test_trailing_stop_requires_position():
    with pytest.raises(ValueError, match="no open position"):
        algos.trailing_stop(FakeExchange(), XBT, percent=Decimal(1), clock=FakeClock(), log=quiet)
