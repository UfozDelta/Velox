from decimal import Decimal

import pytest
from click.testing import CliRunner

from f3l.cli import App, cli
from f3l.config import Settings

from .fakes import FakeClock, FakeExchange


@pytest.fixture
def ex():
    return FakeExchange()


@pytest.fixture
def run(ex):
    def _run(*args, input=None, settings=None, clock=None):
        app = App(exchange=ex, settings=settings or Settings(confirm_above_usd=Decimal(1000)),
                  clock=clock or FakeClock())
        return CliRunner().invoke(cli, list(args), obj=app, input=input)

    return _run


def test_help_needs_no_credentials(monkeypatch):
    for var in ("KuCoinAPI", "KuCoinSecret", "KuCoinPass"):
        monkeypatch.delenv(var, raising=False)
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    for cmd in ("ladder", "chase", "twap", "vwap", "trail", "tpsl", "nuke-all"):
        assert cmd in result.output


def test_price(run):
    result = run("price", "xbtusdtm")
    assert result.exit_code == 0, result.output
    assert "XBTUSDTM" in result.output and "60000.5" in result.output


def test_limit_dry_run_sends_nothing(run, ex):
    result = run("limit", "buy", "XBTUSDTM", "59000.07", "500", "-l", "5", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output and "59000" in result.output
    assert ex.placed == []


def test_small_limit_order_goes_straight_through(run, ex):
    result = run("limit", "buy", "XBTUSDTM", "59000", "500", "--post-only")
    assert result.exit_code == 0, result.output
    assert len(ex.placed) == 1
    req = ex.placed[0]
    assert req.lots == 8 and req.post_only and req.leverage == 1


def test_large_order_asks_for_confirmation(run, ex):
    result = run("limit", "buy", "XBTUSDTM", "59000", "2000", input="n\n")
    assert result.exit_code == 1 and "Aborted" in result.output
    assert ex.placed == []
    result = run("limit", "buy", "XBTUSDTM", "59000", "2000", input="y\n")
    assert result.exit_code == 0 and len(ex.placed) == 1


def test_yes_skips_confirmation(run, ex):
    assert run("limit", "buy", "XBTUSDTM", "59000", "2000", "-y").exit_code == 0
    assert len(ex.placed) == 1


def test_safety_limits_block_orders(run, ex):
    s = Settings(max_leverage=10, max_order_usd=Decimal(1000))
    r = run("limit", "buy", "XBTUSDTM", "59000", "500", "-l", "20", "-y", settings=s)
    assert r.exit_code == 1 and "above your limit of 10x" in r.output
    r = run("limit", "buy", "XBTUSDTM", "59000", "5000", "-y", settings=s)
    assert r.exit_code == 1 and "F3L_MAX_ORDER_USD" in r.output
    assert ex.placed == []


def test_size_below_one_lot(run):
    r = run("limit", "buy", "XBTUSDTM", "60000", "10")
    assert r.exit_code == 2 and "smaller than one lot" in r.output


def test_bad_numbers_rejected(run):
    r = run("limit", "buy", "XBTUSDTM", "abc", "10")
    assert r.exit_code == 2 and "not a number" in r.output


def test_limit_with_lots_iceberg_and_tpsl(run, ex):
    r = run("limit", "sell", "XBTUSDTM", "61000", "20", "--lots", "--iceberg", "5", "--tp", "58000",
            "--sl", "62000", "-y")
    assert r.exit_code == 0, r.output
    req = ex.placed[0]
    assert req.lots == 20 and req.iceberg and req.visible_lots == 5
    assert req.take_profit == Decimal("58000.0") and req.stop_loss == Decimal("62000.0")


def test_tp_on_wrong_side_is_rejected(run, ex):
    r = run("limit", "buy", "XBTUSDTM", "59000", "500", "--tp", "58000")
    assert r.exit_code == 2 and "must be above" in r.output
    assert ex.placed == []


def test_market_order(run, ex):
    r = run("market", "sell", "XBTUSDTM", "600")
    assert r.exit_code == 0, r.output
    assert ex.placed[0].type == "market" and ex.placed[0].lots == 10


def test_stop_direction_is_automatic(run, ex):
    assert run("stop", "sell", "XBTUSDTM", "58000", "300", "--reduce-only").exit_code == 0
    assert run("stop", "buy", "XBTUSDTM", "62000", "300", "--limit", "62100").exit_code == 0
    down, up = ex.placed
    assert down.stop == "down" and down.type == "market" and down.reduce_only
    assert up.stop == "up" and up.type == "limit" and up.price == Decimal("62100.0")


def test_tpsl_on_position(run, ex):
    ex.position = 4
    r = run("tpsl", "XBTUSDTM", "--tp", "65000", "--sl", "58000")
    assert r.exit_code == 0, r.output
    tp, sl = ex.placed
    assert tp.close_order and tp.stop == "up" and tp.stop_price == Decimal("65000.0")
    assert sl.close_order and sl.stop == "down"


def test_tpsl_without_position(run):
    r = run("tpsl", "XBTUSDTM", "--sl", "58000")
    assert r.exit_code == 2 and "no open position" in r.output


def test_ladder_dry_run_preview(run, ex):
    r = run("ladder", "buy", "XBTUSDTM", "60000", "55000", "3000", "-n", "5", "--shape", "linear", "--dry-run")
    assert r.exit_code == 0, r.output
    assert "5 orders" in r.output and "55000" in r.output and "Dry run" in r.output
    assert ex.placed == []


def test_ladder_places_batch_and_exits(run, ex):
    r = run("ladder", "buy", "XBTUSDTM", "60000", "55000", "3000", "-n", "4", "--shape", "exp",
            "--factor", "2", "--sl", "54000", "--tp", "64000", "-y")
    assert r.exit_code == 0, r.output
    rungs = [p for p in ex.placed if p.stop is None]
    exits = [p for p in ex.placed if p.stop]
    assert len(rungs) == 4 and "4/4 orders placed" in r.output
    total = sum(p.lots for p in rungs)
    assert {(e.stop, e.lots, e.reduce_only, e.side) for e in exits} == {("up", total, True, "sell"),
                                                                        ("down", total, True, "sell")}


def test_ladder_sl_inside_range_rejected(run):
    r = run("ladder", "buy", "XBTUSDTM", "60000", "55000", "3000", "-n", "4", "--sl", "57000")
    assert r.exit_code == 2 and "must be below" in r.output


def test_ladder_bad_custom_ratios(run):
    r = run("ladder", "buy", "XBTUSDTM", "60000", "55000", "3000", "-n", "3", "--shape", "custom",
            "--ratios", "1,2")
    assert r.exit_code == 1 and "has 2 values" in r.output


def test_orders_and_positions(run, ex):
    ex.position = -2
    run("limit", "buy", "XBTUSDTM", "59000", "500")
    run("stop", "sell", "XBTUSDTM", "58000", "300")
    r = run("orders")
    assert r.exit_code == 0 and "Open orders" in r.output and "down 58000" in r.output
    r = run("positions")
    assert r.exit_code == 0 and "short" in r.output


def test_nuke_requires_confirmation_and_cancels_stops(run, ex):
    run("limit", "buy", "XBTUSDTM", "59000", "500")
    run("stop", "sell", "XBTUSDTM", "58000", "300")
    r = run("nuke", "XBTUSDTM", input="n\n")
    assert r.exit_code == 1 and not ex.cancelled
    r = run("nuke", "XBTUSDTM", input="y\n")
    assert r.exit_code == 0 and "Cancelled 2 orders" in r.output
    assert not ex.working() and not ex.stops


def test_nuke_all_keep_stops(run, ex):
    run("limit", "buy", "XBTUSDTM", "59000", "500")
    run("limit", "sell", "ETHUSDTM", "4000", "500")
    run("stop", "sell", "XBTUSDTM", "58000", "300")
    r = run("nuke-all", "--keep-stops", "-y")
    assert r.exit_code == 0 and "Cancelled 2 orders on ALL symbols" in r.output
    assert len(ex.stops) == 1


def test_old_commands_point_to_ladder(run):
    r = run("limit-x3", "buy", "XBTUSDTM", "1", "2", "3", "4", "5")
    assert r.exit_code == 2 and "--shape linear" in r.output


def test_chase_command(run, ex):
    clock = FakeClock(on_sleep=lambda t: ex.fill_working())
    r = run("chase", "buy", "XBTUSDTM", "600", "--timeout", "1m", clock=clock)
    assert r.exit_code == 0, r.output
    assert "filled" in r.output and "10 / 10 lots" in r.output


def test_twap_command(run, ex):
    r = run("twap", "sell", "XBTUSDTM", "600", "-d", "5m", "--slices", "5", "-y")
    assert r.exit_code == 0, r.output
    assert len(ex.placed) == 5 and sum(p.lots for p in ex.placed) == 10


def test_twap_dry_run_shows_schedule(run, ex):
    r = run("twap", "buy", "XBTUSDTM", "600", "-d", "1h30m", "--dry-run")
    assert r.exit_code == 0, r.output
    assert "TWAP buy 9 lots of XBTUSDTM over 1h30m in 9 slices" in r.output and ex.placed == []


def test_bad_duration(run):
    r = run("twap", "buy", "XBTUSDTM", "600", "-d", "soon")
    assert r.exit_code == 2 and "not a duration" in r.output


def test_vwap_command_dry_run(run, ex):
    r = run("vwap", "buy", "XBTUSDTM", "600", "-d", "10m", "--slices", "2", "--dry-run")
    assert r.exit_code == 0, r.output
    assert "VWAP" in r.output


def test_trail_command(run, ex):
    ex.position = 3
    ex.mark = Decimal(60000)
    path = iter([61000, 59000])

    def tick(t):
        ex.mark = Decimal(next(path, 59000))
        ex.trigger_stops()

    r = run("trail", "XBTUSDTM", "-p", "1", clock=FakeClock(on_sleep=tick))
    assert r.exit_code == 0, r.output
    assert "59400" in r.output and "Finished: triggered" in r.output


def test_exchange_errors_are_clean(run, ex):
    ex.reject_next = ["Balance insufficient"]
    r = run("limit", "buy", "XBTUSDTM", "59000", "500")
    assert r.exit_code == 1
    assert "Error: [300000] Balance insufficient" in r.output
    assert "Traceback" not in r.output
