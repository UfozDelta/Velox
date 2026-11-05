"""f3l: command-line trading for crypto futures exchanges."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional

import click
import tabulate

from . import algos, ladder
from .config import SafetyError, Settings
from .exchange import EXCHANGES, AbstractExchange, ExchangeError
from .models import BUY, SELL, D, Contract, OrderRequest, fmt, opposite

TABLE_FMT = "rounded_outline"
TRIGGERS = {"mark": "MP", "last": "TP", "index": "IP"}


# ------------------------------------------------------------------ plumbing
@dataclass
class App:
    """Shared state for one CLI run. Tests pass in their own."""

    exchange_name: str = "kucoin"
    exchange: Optional[AbstractExchange] = None
    settings: Optional[Settings] = None
    clock: algos.Clock = field(default_factory=algos.Clock)

    @property
    def ex(self) -> AbstractExchange:
        # Created on first use, so `--help` never needs credentials.
        if self.exchange is None:
            self.exchange = EXCHANGES[self.exchange_name]()
        return self.exchange

    @property
    def cfg(self) -> Settings:
        if self.settings is None:
            self.settings = Settings.from_env()
        return self.settings


class F3lGroup(click.Group):
    """Turns expected errors into clean one-line messages instead of tracebacks."""

    def invoke(self, ctx: click.Context):
        try:
            return super().invoke(ctx)
        except (ExchangeError, SafetyError, ValueError) as exc:
            raise click.ClickException(str(exc)) from None


class DecimalType(click.ParamType):
    name = "number"

    def __init__(self, positive: bool = True):
        self.positive = positive

    def convert(self, value, param, ctx):
        if isinstance(value, Decimal):
            return value
        try:
            number = Decimal(str(value).replace(",", "").replace("$", ""))
        except InvalidOperation:
            self.fail(f"{value!r} is not a number", param, ctx)
        if not number.is_finite() or (self.positive and number <= 0):
            self.fail(f"{value!r} must be a positive number", param, ctx)
        return number


class DurationType(click.ParamType):
    """Accepts 90, 90s, 15m, 2h, 1h30m."""

    name = "duration"
    UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

    def convert(self, value, param, ctx):
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip().lower()
        if re.fullmatch(r"\d+(\.\d+)?", text):
            return float(text)
        parts = re.findall(r"(\d+(?:\.\d+)?)([smhd])", text)
        if not parts or "".join(n + u for n, u in parts) != text:
            self.fail(f"{value!r} is not a duration (examples: 90s, 15m, 2h, 1h30m)", param, ctx)
        seconds = sum(float(n) * self.UNITS[u] for n, u in parts)
        if seconds <= 0:
            self.fail("duration must be positive", param, ctx)
        return seconds


class RatiosType(click.ParamType):
    name = "ratios"

    def convert(self, value, param, ctx):
        if isinstance(value, list):
            return value
        try:
            return [Decimal(x) for x in str(value).replace(":", ",").split(",") if x.strip()]
        except InvalidOperation:
            self.fail(f"{value!r} should look like 1,2,3", param, ctx)


NUMBER = DecimalType()
DURATION = DurationType()
SIDE = click.Choice([BUY, SELL], case_sensitive=False)


def _upper(ctx, param, value):
    return value.upper() if isinstance(value, str) else value


def symbol_arg(default: Optional[str] = None, required: bool = True):
    if default is not None:
        return click.argument("symbol", default=default, callback=_upper)
    return click.argument("symbol", callback=_upper, required=required)


def leverage_option(f):
    return click.option("-l", "--leverage", type=click.IntRange(min=1), default=None,
                        help="Leverage (default: F3L_DEFAULT_LEVERAGE, else 1).")(f)


def safety_options(f):
    f = click.option("-y", "--yes", is_flag=True, help="Skip the confirmation prompt.")(f)
    f = click.option("--dry-run", is_flag=True, help="Show what would be sent, send nothing.")(f)
    return f


def margin_option(f):
    return click.option("--margin-mode", type=click.Choice(["isolated", "cross"], case_sensitive=False),
                        default=None, help="Margin mode for the order (exchange default if omitted).")(f)


def lots_option(f):
    return click.option("--lots", "in_lots", is_flag=True,
                        help="SIZE is a number of contracts (lots) instead of USD.")(f)


def trigger_option(f):
    return click.option("--trigger", type=click.Choice(list(TRIGGERS), case_sensitive=False), default="mark",
                        show_default=True, help="Price that triggers stops: mark, last trade or index.")(f)


def tpsl_options(f):
    f = click.option("--sl", "stop_loss", type=NUMBER, default=None, help="Attach a stop-loss at this price.")(f)
    f = click.option("--tp", "take_profit", type=NUMBER, default=None, help="Attach a take-profit at this price.")(f)
    return f


def table(rows, headers="keys") -> str:
    return tabulate.tabulate(rows, headers=headers, tablefmt=TABLE_FMT, disable_numparse=True)


def kv_table(data: dict) -> str:
    return table([{"Property": k, "Value": v} for k, v in data.items()])


def human_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    parts = []
    for unit, size in (("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size or (unit == "s" and not parts):
            parts.append(f"{seconds // size}{unit}")
            seconds %= size
    return "".join(parts)


def money(value: Decimal) -> str:
    return f"${D(value).quantize(Decimal('0.01')):,}"


def leverage_for(app: App, leverage: Optional[int]) -> int:
    return leverage if leverage is not None else app.cfg.default_leverage


def resolve_lots(contract: Contract, size: Decimal, price: Decimal, in_lots: bool) -> int:
    """Convert the user's SIZE (USD by default) to whole lots."""
    if in_lots:
        if size != size.to_integral_value():
            raise click.BadParameter("with --lots, SIZE must be a whole number", param_hint="SIZE")
        lots = int(size)
        if lots % contract.lot_size:
            raise click.BadParameter(f"SIZE must be a multiple of the lot size {contract.lot_size}",
                                     param_hint="SIZE")
    else:
        lots = contract.usd_to_lots(size, price)
    if lots <= 0:
        one_lot = contract.lot_value(price) * contract.lot_size
        raise click.BadParameter(
            f"{money(size)} is smaller than one lot of {contract.symbol} ({money(one_lot)} at {fmt(price)})",
            param_hint="SIZE")
    if contract.max_order_lots and lots > contract.max_order_lots:
        raise click.BadParameter(f"{lots} lots is above the exchange maximum of {contract.max_order_lots}",
                                 param_hint="SIZE")
    return lots


def check_tpsl(side: str, ref_price: Decimal, take_profit: Optional[Decimal], stop_loss: Optional[Decimal]) -> None:
    """Make sure TP is on the profit side and SL on the loss side of the entry."""
    long = side == BUY
    if take_profit is not None and (take_profit <= ref_price if long else take_profit >= ref_price):
        raise click.UsageError(f"--tp {fmt(take_profit)} must be {'above' if long else 'below'} "
                               f"the entry price {fmt(ref_price)} for a {side}")
    if stop_loss is not None and (stop_loss >= ref_price if long else stop_loss <= ref_price):
        raise click.UsageError(f"--sl {fmt(stop_loss)} must be {'below' if long else 'above'} "
                               f"the entry price {fmt(ref_price)} for a {side}")


def preview_and_confirm(app: App, rows: list[dict], total_usd: Decimal, leverage: Optional[int],
                        contract: Optional[Contract], *, dry_run: bool, yes: bool,
                        always_confirm: bool = False, prompt: str = "Place this order?") -> bool:
    """Run safety checks, show the preview, then ask (if needed).

    Returns False for a dry run, True if the caller should go ahead.
    """
    app.cfg.check(total_usd, leverage, contract.max_leverage if contract else None)
    if rows:
        click.echo(table(rows))
    if total_usd > 0:
        line = f"Total: {money(total_usd)} notional"
        if leverage:
            line += f" at {leverage}x (about {money(total_usd / leverage)} margin)"
        click.echo(line)
    if dry_run:
        click.echo("Dry run: nothing was sent.")
        return False
    if yes:
        return True
    if always_confirm or app.cfg.needs_confirmation(total_usd):
        click.confirm(prompt, abort=True)
    return True


def stop_direction(trigger_price: Decimal, current: Decimal) -> str:
    return "up" if trigger_price > current else "down"


def trigger_reference(app: App, symbol: str, trigger: str) -> Decimal:
    if TRIGGERS[trigger] == "MP":
        return D(app.ex.mark_price(symbol)["value"])
    if TRIGGERS[trigger] == "IP":
        return D(app.ex.mark_price(symbol).get("indexPrice") or app.ex.ticker(symbol).last)
    return app.ex.ticker(symbol).last


def print_exec_result(result: algos.ExecResult, contract: Contract) -> None:
    rows = {
        "Status": result.status,
        "Filled": f"{result.filled_lots} / {result.requested_lots} lots",
        "Filled value": money(result.filled_value),
        "Average price": fmt(result.avg_price) if result.avg_price else "-",
        "Orders sent": result.orders_placed,
    }
    click.echo(kv_table(rows))


# --------------------------------------------------------------------- group
@click.group(cls=F3lGroup, context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--exchange", type=click.Choice(sorted(EXCHANGES), case_sensitive=False), default="kucoin",
              show_default=True, help="Exchange to trade on.")
@click.pass_context
def cli(ctx: click.Context, exchange: str):
    """Trade crypto futures from the terminal.

    SIZE is in USD unless you pass --lots. Orders above F3L_CONFIRM_ABOVE_USD
    ask for confirmation; every order command supports --dry-run.
    """
    if ctx.obj is None:
        ctx.obj = App(exchange_name=exchange.lower())


pass_app = click.make_pass_decorator(App)


# ------------------------------------------------------------- market info
@cli.command()
@symbol_arg(default="XBTUSDTM")
@pass_app
def price(app: App, symbol: str):
    """Mark price, last trade and best bid/ask for SYMBOL (default XBTUSDTM)."""
    mark = app.ex.mark_price(symbol)
    t = app.ex.ticker(symbol)
    click.echo(kv_table({
        "Symbol": symbol,
        "Mark price": fmt(D(mark["value"])),
        "Index price": fmt(D(mark["indexPrice"])) if mark.get("indexPrice") is not None else "-",
        "Last trade": fmt(t.last),
        "Best bid": fmt(t.bid),
        "Best ask": fmt(t.ask),
        "Spread": fmt(t.ask - t.bid),
    }))


@cli.command()
@symbol_arg()
@pass_app
def contract(app: App, symbol: str):
    """Trading rules for SYMBOL: tick size, lot size and minimum order."""
    c = app.ex.contract(symbol)
    t = app.ex.ticker(symbol)
    click.echo(kv_table({
        "Symbol": c.symbol,
        "Tick size": fmt(c.tick_size),
        "Lot size": c.lot_size,
        "Multiplier": fmt(c.multiplier),
        "Inverse": "yes" if c.is_inverse else "no",
        "Max leverage": c.max_leverage or "-",
        "Value of one lot now": money(c.lot_value(t.last) * c.lot_size),
    }))


# ----------------------------------------------------------------- account
@cli.command()
@pass_app
def balance(app: App):
    """USDT futures account balance."""
    click.echo(kv_table(app.ex.balance()))


def _order_rows(orders: list[dict], stops: bool = False) -> list[dict]:
    rows = []
    for o in orders:
        flags = [name for key, name in (("postOnly", "post-only"), ("hidden", "hidden"), ("iceberg", "iceberg"),
                                        ("reduceOnly", "reduce-only"), ("closeOrder", "close")) if o.get(key)]
        row = {
            "ID": o.get("id"),
            "Symbol": o.get("symbol"),
            "Side": o.get("side") or "-",
            "Type": o.get("type"),
            "Price": o.get("price") or "market",
            "Lots": o.get("size") or "all",
        }
        if stops:
            row["Trigger"] = f"{o.get('stop')} {o.get('stopPrice')} ({o.get('stopPriceType')})"
        else:
            row["Filled"] = o.get("filledSize", o.get("dealSize", 0))
            row["Value"] = o.get("value")
        row["Flags"] = ", ".join(flags)
        rows.append(row)
    return rows


@cli.command()
@symbol_arg(required=False)
@pass_app
def orders(app: App, symbol: Optional[str]):
    """Open orders and untriggered stop orders (optionally for one SYMBOL)."""
    open_orders = app.ex.open_orders(symbol)
    stop_orders = app.ex.stop_orders(symbol)
    click.echo("Open orders:")
    click.echo(table(_order_rows(open_orders)) if open_orders else "  none")
    click.echo("Stop orders:")
    click.echo(table(_order_rows(stop_orders, stops=True)) if stop_orders else "  none")


@cli.command()
@symbol_arg(required=False)
@pass_app
def positions(app: App, symbol: Optional[str]):
    """Open positions (optionally for one SYMBOL)."""
    rows = []
    for p in app.ex.positions(symbol):
        qty = int(p.get("currentQty") or 0)
        if not qty:
            continue
        rows.append({
            "Symbol": p.get("symbol"),
            "Side": "long" if qty > 0 else "short",
            "Lots": abs(qty),
            "Entry": p.get("avgEntryPrice"),
            "Mark": p.get("markPrice"),
            "Liquidation": p.get("liquidationPrice"),
            "Unrealised PnL": p.get("unrealisedPnl"),
            "ROE %": f"{float(p.get('unrealisedRoePcnt') or 0) * 100:.2f}",
            "Leverage": p.get("realLeverage") or p.get("leverage"),
            "Margin": (p.get("marginMode") or ("CROSS" if p.get("crossMode") else "ISOLATED")),
        })
    click.echo(table(rows) if rows else "No open positions.")


# ------------------------------------------------------------ basic orders
@cli.command()
@click.argument("side", type=SIDE, callback=lambda c, p, v: v.lower())
@symbol_arg()
@click.argument("price", type=NUMBER)
@click.argument("size", type=NUMBER)
@lots_option
@leverage_option
@click.option("--post-only", is_flag=True, help="Only add liquidity (rejected if it would fill immediately).")
@click.option("--hidden", is_flag=True, help="Hide the order from the order book.")
@click.option("--iceberg", "visible", type=NUMBER, default=None,
              help="Iceberg: only show this much at a time (same unit as SIZE).")
@click.option("--reduce-only", is_flag=True, help="Only reduce an existing position.")
@click.option("--ioc", is_flag=True, help="Immediate-or-cancel: fill what you can now, cancel the rest.")
@tpsl_options
@trigger_option
@margin_option
@safety_options
@pass_app
def limit(app: App, side, symbol, price, size, in_lots, leverage, post_only, hidden, visible, reduce_only,
          ioc, take_profit, stop_loss, trigger, margin_mode, dry_run, yes):
    """Place one limit order: SIDE SYMBOL PRICE SIZE.

    \b
    Examples:
      f3l limit buy XBTUSDTM 60000 500 -l 5
      f3l limit sell ETHUSDTM 4000 2000 --iceberg 200 --sl 4200
    """
    c = app.ex.contract(symbol)
    px = c.round_price(price, side)
    lots = resolve_lots(c, size, px, in_lots)
    visible_lots = resolve_lots(c, visible, px, in_lots) if visible is not None else None
    if visible_lots is not None and visible_lots >= lots:
        raise click.UsageError("--iceberg visible size must be smaller than the order size")
    check_tpsl(side, px, take_profit, stop_loss)
    lev = leverage_for(app, leverage)
    req = OrderRequest(side=side, symbol=symbol, lots=lots, type="limit", price=px, leverage=lev,
                       post_only=post_only, hidden=hidden, iceberg=visible_lots is not None,
                       visible_lots=visible_lots, reduce_only=reduce_only,
                       time_in_force="IOC" if ioc else None,
                       take_profit=c.round_price(take_profit) if take_profit else None,
                       stop_loss=c.round_price(stop_loss) if stop_loss else None,
                       stop_price_type=TRIGGERS[trigger], margin_mode=margin_mode)
    req.validate()
    usd = c.lots_to_usd(lots, px)
    rows = [{"Side": side, "Symbol": symbol, "Price": fmt(px), "Lots": lots, "Value": money(usd),
             "Type": req.describe()}]
    if not preview_and_confirm(app, rows, usd, lev, c, dry_run=dry_run, yes=yes):
        return
    order_id = app.ex.place_order(req)
    click.echo(f"Order placed: {order_id}")


@cli.command()
@click.argument("side", type=SIDE, callback=lambda c, p, v: v.lower())
@symbol_arg()
@click.argument("size", type=NUMBER)
@lots_option
@leverage_option
@click.option("--reduce-only", is_flag=True, help="Only reduce an existing position.")
@tpsl_options
@trigger_option
@margin_option
@safety_options
@pass_app
def market(app: App, side, symbol, size, in_lots, leverage, reduce_only, take_profit, stop_loss, trigger,
           margin_mode, dry_run, yes):
    """Place a market order: SIDE SYMBOL SIZE."""
    c = app.ex.contract(symbol)
    t = app.ex.ticker(symbol)
    ref = t.ask if side == BUY else t.bid
    lots = resolve_lots(c, size, ref, in_lots)
    check_tpsl(side, ref, take_profit, stop_loss)
    lev = leverage_for(app, leverage)
    req = OrderRequest(side=side, symbol=symbol, lots=lots, type="market", leverage=lev,
                       reduce_only=reduce_only,
                       take_profit=c.round_price(take_profit) if take_profit else None,
                       stop_loss=c.round_price(stop_loss) if stop_loss else None,
                       stop_price_type=TRIGGERS[trigger], margin_mode=margin_mode)
    usd = c.lots_to_usd(lots, ref)
    rows = [{"Side": side, "Symbol": symbol, "Est. price": fmt(ref), "Lots": lots, "Value": money(usd),
             "Type": req.describe()}]
    if not preview_and_confirm(app, rows, usd, lev, c, dry_run=dry_run, yes=yes):
        return
    click.echo(f"Order placed: {app.ex.place_order(req)}")


@cli.command()
@click.argument("side", type=SIDE, callback=lambda c, p, v: v.lower())
@symbol_arg()
@click.argument("trigger_price", metavar="TRIGGER", type=NUMBER)
@click.argument("size", type=NUMBER)
@click.option("--limit", "limit_price", type=NUMBER, default=None,
              help="Send a limit order at this price when triggered (default: market).")
@lots_option
@leverage_option
@click.option("--reduce-only", is_flag=True, help="Only reduce an existing position.")
@trigger_option
@margin_option
@safety_options
@pass_app
def stop(app: App, side, symbol, trigger_price, size, limit_price, in_lots, leverage, reduce_only, trigger,
         margin_mode, dry_run, yes):
    """Stop order: when price hits TRIGGER, send a market (or --limit) order.

    Fires when price rises to TRIGGER if TRIGGER is above the current price,
    or falls to it if below.
    """
    c = app.ex.contract(symbol)
    current = trigger_reference(app, symbol, trigger)
    trig = c.round_price(trigger_price)
    if trig == current:
        raise click.UsageError("TRIGGER equals the current price; it would fire immediately")
    ref = c.round_price(limit_price, side) if limit_price else trig
    lots = resolve_lots(c, size, ref, in_lots)
    lev = leverage_for(app, leverage)
    direction = stop_direction(trig, current)
    req = OrderRequest(side=side, symbol=symbol, lots=lots, type="limit" if limit_price else "market",
                       price=ref if limit_price else None, leverage=lev, reduce_only=reduce_only,
                       stop=direction, stop_price=trig, stop_price_type=TRIGGERS[trigger],
                       margin_mode=margin_mode)
    usd = c.lots_to_usd(lots, ref)
    rows = [{"Side": side, "Symbol": symbol, "Fires when": f"{trigger} price {'>=' if direction == 'up' else '<='} {fmt(trig)}",
             "Then": f"limit @ {fmt(ref)}" if limit_price else "market", "Lots": lots, "Value": money(usd)}]
    if not preview_and_confirm(app, rows, usd, lev, c, dry_run=dry_run, yes=yes):
        return
    click.echo(f"Stop order placed: {app.ex.place_order(req)}")


@cli.command()
@symbol_arg()
@tpsl_options
@trigger_option
@safety_options
@pass_app
def tpsl(app: App, symbol, take_profit, stop_loss, trigger, dry_run, yes):
    """Set a take-profit and/or stop-loss on your open SYMBOL position.

    Each one closes the whole position when triggered.
    """
    if take_profit is None and stop_loss is None:
        raise click.UsageError("give --tp, --sl or both")
    c = app.ex.contract(symbol)
    qty = app.ex.position_qty(symbol)
    if qty == 0:
        raise click.UsageError(f"no open position on {symbol}")
    side = BUY if qty > 0 else SELL
    current = trigger_reference(app, symbol, trigger)
    check_tpsl(side, current, take_profit, stop_loss)
    reqs = []
    rows = []
    for label, price in (("take-profit", take_profit), ("stop-loss", stop_loss)):
        if price is None:
            continue
        px = c.round_price(price)
        direction = stop_direction(px, current)
        reqs.append(OrderRequest(side=None, symbol=symbol, type="market", close_order=True, stop=direction,
                                 stop_price=px, stop_price_type=TRIGGERS[trigger]))
        rows.append({"Order": label, "Fires when": f"{trigger} price {'>=' if direction == 'up' else '<='} {fmt(px)}",
                     "Closes": f"whole {'long' if qty > 0 else 'short'} ({abs(qty)} lots)"})
    if not preview_and_confirm(app, rows, Decimal(0), None, c, dry_run=dry_run, yes=yes):
        return
    for req, row in zip(reqs, rows):
        click.echo(f"{row['Order']} placed: {app.ex.place_order(req)}")


# ------------------------------------------------------------------- ladder
@cli.command(name="ladder")
@click.argument("side", type=SIDE, callback=lambda c, p, v: v.lower())
@symbol_arg()
@click.argument("start", metavar="FROM", type=NUMBER)
@click.argument("end", metavar="TO", type=NUMBER)
@click.argument("size", type=NUMBER)
@click.option("-n", "--orders", "count", type=click.IntRange(1, 200), required=True, help="Number of orders.")
@click.option("--shape", type=click.Choice(ladder.SHAPES), default="even", show_default=True,
              help="How size is spread: even, linear (1:2:3...), exp (x factor each step) or custom.")
@click.option("--factor", type=NUMBER, default=Decimal("1.5"), show_default=True, help="Growth per step for --shape exp.")
@click.option("--ratios", type=RatiosType(), default=None, help="Sizes for --shape custom, e.g. 1,1,2,3.")
@click.option("--spacing", type=click.Choice(ladder.SPACINGS), default="linear", show_default=True,
              help="linear = equal $ gaps, geometric = equal % gaps.")
@click.option("--reverse", is_flag=True, help="Put the biggest orders at FROM instead of TO.")
@leverage_option
@click.option("--post-only", is_flag=True, help="Only add liquidity.")
@click.option("--hidden", is_flag=True, help="Hide the orders from the order book.")
@click.option("--reduce-only", is_flag=True, help="Only reduce an existing position.")
@tpsl_options
@trigger_option
@margin_option
@safety_options
@pass_app
def ladder_cmd(app: App, side, symbol, start, end, size, count, shape, factor, ratios, spacing, reverse, leverage,
               post_only, hidden, reduce_only, take_profit, stop_loss, trigger, margin_mode, dry_run, yes):
    """Spread SIZE (USD) over --orders limit orders from FROM to TO.

    By default sizes grow from FROM towards TO, so for a buy ladder put the
    price nearest the market first.

    \b
    Examples:
      f3l ladder buy XBTUSDTM 60000 55000 1000 -n 5                 # 5 equal orders
      f3l ladder buy XBTUSDTM 60000 55000 1000 -n 5 --shape linear  # 1:2:3:4:5
      f3l ladder sell ETHUSDTM 4000 4400 2000 -n 8 --shape exp --factor 1.3 --sl 4600
    """
    if post_only and hidden:
        raise click.UsageError("--post-only cannot be combined with --hidden")
    c = app.ex.contract(symbol)
    rungs = ladder.plan(c, side, start, end, size, count, shape, factor, ratios, spacing, reverse)
    lev = leverage_for(app, leverage)
    # Check TP/SL against the whole ladder: TP beyond the best rung, SL beyond the worst.
    best = max(r.price for r in rungs) if side == BUY else min(r.price for r in rungs)
    worst = min(r.price for r in rungs) if side == BUY else max(r.price for r in rungs)
    check_tpsl(side, best, take_profit, None)
    check_tpsl(side, worst, None, stop_loss)

    reqs = [OrderRequest(side=side, symbol=symbol, lots=r.lots, type="limit", price=r.price, leverage=lev,
                         post_only=post_only, hidden=hidden, reduce_only=reduce_only, margin_mode=margin_mode)
            for r in rungs]
    total_lots = sum(r.lots for r in rungs)
    total_usd = sum((r.usd for r in rungs), Decimal(0))
    rows = [{"#": i, "Price": fmt(r.price), "Lots": r.lots, "Value": money(r.usd)} for i, r in enumerate(rungs, 1)]

    exits = []
    for label, price in (("take-profit", take_profit), ("stop-loss", stop_loss)):
        if price is not None:
            px = c.round_price(price)
            long = side == BUY
            up = (label == "take-profit") == long
            exits.append((label, OrderRequest(side=opposite(side), symbol=symbol, lots=total_lots, type="market",
                                              reduce_only=True, stop="up" if up else "down", stop_price=px,
                                              stop_price_type=TRIGGERS[trigger], leverage=lev,
                                              margin_mode=margin_mode)))
            rows.append({"#": label, "Price": fmt(px), "Lots": total_lots, "Value": "reduce-only stop"})

    avg = sum((r.price * r.lots for r in rungs), Decimal(0)) / total_lots
    click.echo(f"{side} ladder on {symbol}: {len(rungs)} orders, {total_lots} lots, "
               f"average price if all fill {fmt(c.round_price(avg))}")
    if not preview_and_confirm(app, rows, total_usd, lev, c, dry_run=dry_run, yes=yes,
                               prompt=f"Place these {len(rungs)} orders?"):
        return
    results = app.ex.place_orders(reqs)
    ok = sum(1 for r in results if "order_id" in r)
    out = [{"#": i, "Price": fmt(r.price), "Lots": r.lots, "Result": res.get("order_id") or res.get("error")}
           for i, (r, res) in enumerate(zip(rungs, results), 1)]
    click.echo(table(out))
    click.echo(f"{ok}/{len(rungs)} orders placed.")
    if exits and ok:
        for label, req in exits:
            try:
                click.echo(f"{label} placed: {app.ex.place_order(req)}")
            except ExchangeError as exc:
                click.echo(f"{label} FAILED: {exc}", err=True)
    elif exits:
        click.echo("No ladder orders were placed, so take-profit/stop-loss were skipped.", err=True)


# ----------------------------------------------------------------- cancels
@cli.command()
@click.argument("order_id")
@pass_app
def cancel(app: App, order_id: str):
    """Cancel one order by ORDER_ID."""
    app.ex.cancel_order(order_id)
    click.echo(f"Cancelled {order_id}")


def _nuke(app: App, symbol: Optional[str], keep_stops: bool, yes: bool) -> None:
    where = symbol or "ALL symbols"
    what = "open orders" if keep_stops else "open orders and stop orders (including TP/SL)"
    if not yes:
        click.confirm(f"Cancel all {what} on {where}?", abort=True)
    cancelled = [{"Order ID": oid, "Kind": "limit"} for oid in app.ex.cancel_all(symbol)]
    if not keep_stops:
        cancelled += [{"Order ID": oid, "Kind": "stop"} for oid in app.ex.cancel_all_stops(symbol)]
    click.echo(table(cancelled) if cancelled else "Nothing to cancel.")
    if cancelled:
        click.echo(f"Cancelled {len(cancelled)} orders on {where}.")


@cli.command()
@symbol_arg()
@click.option("--keep-stops", is_flag=True, help="Leave stop / TP / SL orders in place.")
@click.option("-y", "--yes", is_flag=True, help="Skip the confirmation prompt.")
@pass_app
def nuke(app: App, symbol: str, keep_stops: bool, yes: bool):
    """Cancel every order on SYMBOL (stop orders too, unless --keep-stops)."""
    _nuke(app, symbol, keep_stops, yes)


@cli.command(name="nuke-all")
@click.option("--keep-stops", is_flag=True, help="Leave stop / TP / SL orders in place.")
@click.option("-y", "--yes", is_flag=True, help="Skip the confirmation prompt.")
@pass_app
def nuke_all(app: App, keep_stops: bool, yes: bool):
    """Cancel every order on every symbol (stop orders too, unless --keep-stops)."""
    _nuke(app, None, keep_stops, yes)


# ---------------------------------------------------------- algo orders
@cli.command()
@click.argument("side", type=SIDE, callback=lambda c, p, v: v.lower())
@symbol_arg()
@click.argument("size", type=NUMBER)
@lots_option
@leverage_option
@click.option("--max-price", "cap", type=NUMBER, default=None,
              help="Never chase beyond this price (max for buys, min for sells).")
@click.option("--timeout", type=DURATION, default=None, help="Give up after this long, e.g. 5m.")
@click.option("--then-market", is_flag=True, help="On timeout, send whatever is left at market.")
@click.option("--interval", type=click.FloatRange(0.2), default=1.0, show_default=True,
              help="Seconds between price checks.")
@click.option("--offset", "offset_ticks", type=click.IntRange(0), default=0, show_default=True,
              help="Ticks inside the spread to place (0 = join best bid/ask).")
@click.option("--reduce-only", is_flag=True, help="Only reduce an existing position.")
@margin_option
@safety_options
@pass_app
def chase(app: App, side, symbol, size, in_lots, leverage, cap, timeout, then_market, interval, offset_ticks,
          reduce_only, margin_mode, dry_run, yes):
    """Chase the best price with a post-only limit order until it fills.

    The order sits at the best bid (buy) or ask (sell) and is re-priced
    whenever the market moves away. You pay maker fees. Ctrl+C cancels.
    """
    c = app.ex.contract(symbol)
    t = app.ex.ticker(symbol)
    start_px = algos.chase_price(c, side, t, offset_ticks, cap)
    lots = resolve_lots(c, size, start_px, in_lots)
    lev = leverage_for(app, leverage)
    usd = c.lots_to_usd(lots, start_px)
    rows = [{"Side": side, "Symbol": symbol, "Lots": lots, "Value": money(usd), "Start price": fmt(start_px),
             "Cap": fmt(cap) if cap else "none", "Timeout": human_duration(timeout) if timeout else "none",
             "On timeout": "market" if then_market else "cancel"}]
    if not preview_and_confirm(app, rows, usd, lev, c, dry_run=dry_run, yes=yes, prompt="Start chasing?"):
        return
    click.echo("Chasing (Ctrl+C to stop)...")
    result = algos.chase(app.ex, c, side, lots, leverage=lev, limit_price=cap, timeout=timeout,
                         interval=interval, offset_ticks=offset_ticks, reduce_only=reduce_only,
                         margin_mode=margin_mode, on_timeout="market" if then_market else "cancel",
                         clock=app.clock, log=click.echo)
    print_exec_result(result, c)


def _sliced(app: App, kind: str, side, symbol, size, in_lots, leverage, duration, slices, cap, reduce_only,
            margin_mode, dry_run, yes, lookback_days: int = 7):
    c = app.ex.contract(symbol)
    t = app.ex.ticker(symbol)
    ref = t.ask if side == BUY else t.bid
    lots = resolve_lots(c, size, ref, in_lots)
    if slices is None:
        slices = max(2, min(60, int(duration // 60)))
    if lots < slices * c.lot_size:
        slices = max(1, lots // c.lot_size)
        click.echo(f"Order is only {lots} lots, so using {slices} slices.")
    if kind == "vwap":
        weights = algos.volume_profile(app.ex, symbol, time.time(), duration, slices, lookback_days)
    else:
        weights = [Decimal(1)] * slices
    schedule = algos.make_schedule(lots, duration, weights, c.lot_size)
    lev = leverage_for(app, leverage)
    usd = c.lots_to_usd(lots, ref)
    rows = [{"Slice": i, "Starts after": human_duration(s.offset), "Lots": s.lots,
             "Share": f"{s.lots / lots:.1%}"} for i, s in enumerate(schedule, 1)]
    click.echo(f"{kind.upper()} {side} {lots} lots of {symbol} over {human_duration(duration)} in {len(schedule)} slices; "
               f"{'IOC limit at ' + fmt(cap) if cap else 'market orders'}")
    if not preview_and_confirm(app, rows, usd, lev, c, dry_run=dry_run, yes=yes, prompt=f"Start {kind.upper()}?"):
        return
    click.echo("Running (Ctrl+C to stop)...")
    result = algos.run_schedule(app.ex, c, side, schedule, leverage=lev, limit_price=cap, reduce_only=reduce_only,
                                margin_mode=margin_mode, clock=app.clock, log=click.echo)
    print_exec_result(result, c)


def sliced_options(f):
    f = click.option("--reduce-only", is_flag=True, help="Only reduce an existing position.")(f)
    f = click.option("--limit-price", "cap", type=NUMBER, default=None,
                     help="Worst acceptable price; slices become IOC limits at this price.")(f)
    f = click.option("--slices", type=click.IntRange(1, 500), default=None,
                     help="Number of child orders (default: one per minute, 2-60).")(f)
    f = click.option("-d", "--duration", type=DURATION, required=True, help="Total time, e.g. 30m or 2h.")(f)
    return f


@cli.command()
@click.argument("side", type=SIDE, callback=lambda c, p, v: v.lower())
@symbol_arg()
@click.argument("size", type=NUMBER)
@lots_option
@leverage_option
@sliced_options
@margin_option
@safety_options
@pass_app
def twap(app: App, side, symbol, size, in_lots, leverage, duration, slices, cap, reduce_only, margin_mode,
         dry_run, yes):
    """Split SIZE into equal slices sent evenly over --duration."""
    _sliced(app, "twap", side, symbol, size, in_lots, leverage, duration, slices, cap, reduce_only, margin_mode,
            dry_run, yes)


@cli.command()
@click.argument("side", type=SIDE, callback=lambda c, p, v: v.lower())
@symbol_arg()
@click.argument("size", type=NUMBER)
@lots_option
@leverage_option
@sliced_options
@click.option("--lookback-days", type=click.IntRange(1, 30), default=7, show_default=True,
              help="Days of history used to build the volume profile.")
@margin_option
@safety_options
@pass_app
def vwap(app: App, side, symbol, size, in_lots, leverage, duration, slices, cap, reduce_only, lookback_days,
         margin_mode, dry_run, yes):
    """Like TWAP, but slices are sized by typical volume at this time of day.

    Uses the same time window on each of the last --lookback-days days, so
    more is sent when the market is usually busier.
    """
    _sliced(app, "vwap", side, symbol, size, in_lots, leverage, duration, slices, cap, reduce_only, margin_mode,
            dry_run, yes, lookback_days)


@cli.command()
@symbol_arg()
@click.option("-p", "--percent", type=NUMBER, required=True, help="Trail distance in percent, e.g. 2.")
@click.option("--activation", type=NUMBER, default=None,
              help="Only start trailing once price reaches this level.")
@click.option("--interval", type=click.FloatRange(0.5), default=2.0, show_default=True,
              help="Seconds between price checks.")
@trigger_option
@click.option("--cancel-on-exit", is_flag=True, help="Remove the stop when this command exits.")
@click.option("--dry-run", is_flag=True, help="Show the first stop level, send nothing.")
@pass_app
def trail(app: App, symbol, percent, activation, interval, trigger, cancel_on_exit, dry_run):
    """Trailing stop on your open SYMBOL position.

    Keeps a real stop order on KuCoin and moves it as the price improves,
    so you stay protected even if this command dies. When you exit, the
    last stop stays in place unless you pass --cancel-on-exit.
    """
    if percent >= 100:
        raise click.BadParameter("must be below 100", param_hint="--percent")
    c = app.ex.contract(symbol)
    qty = app.ex.position_qty(symbol)
    if qty == 0:
        raise click.UsageError(f"no open position on {symbol}")
    current = trigger_reference(app, symbol, trigger)
    level = algos.trail_level(c, qty > 0, current, percent)
    click.echo(kv_table({
        "Position": f"{'long' if qty > 0 else 'short'} {abs(qty)} lots",
        f"Current {trigger} price": fmt(current),
        "First stop": fmt(level) if activation is None else f"after price reaches {fmt(activation)}",
        "Trail": f"{fmt(percent)}%",
    }))
    if dry_run:
        click.echo("Dry run: nothing was sent.")
        return
    click.echo("Trailing (Ctrl+C to stop)...")
    result = algos.trailing_stop(app.ex, c, percent=percent, activation=activation, interval=interval,
                                 trigger=TRIGGERS[trigger], cancel_on_exit=cancel_on_exit,
                                 clock=app.clock, log=click.echo)
    click.echo(f"Finished: {result.status}")


# ------------------------------------------------------ renamed commands
def _moved(old: str, hint: str):
    @cli.command(name=old, hidden=True, context_settings={"ignore_unknown_options": True})
    @click.argument("args", nargs=-1, type=click.UNPROCESSED)
    def _cmd(args):
        raise click.UsageError(f"'{old}' was replaced by: {hint}")

    return _cmd


_moved("limit-batch", "f3l ladder SIDE SYMBOL FROM TO SIZE -n ORDERS")
_moved("limit-x3", "f3l ladder SIDE SYMBOL FROM TO SIZE -n ORDERS --shape linear")


if __name__ == "__main__":
    cli()
