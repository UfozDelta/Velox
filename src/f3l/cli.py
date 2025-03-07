import click
from .exchange import KuCoin, AbstractExchange

EXCHANGE_INSTANCES = {
    "kucoin": KuCoin(),
}

@click.group()
@click.option("--exchange", type=click.Choice(["kucoin"], case_sensitive=False), default="kucoin", help="Choose exchange")
@click.pass_context
def cli(ctx, exchange : AbstractExchange):
    """CLI tool to interact with crypto exchanges."""
    ctx.obj = EXCHANGE_INSTANCES[exchange]

@cli.command()
@click.argument("symbol", default="XBTUSDTM")
@click.pass_obj
def price(exchange: AbstractExchange, symbol: str):
    """Fetch market price from the selected exchange."""
    result = exchange.price(symbol.upper())
    click.echo(result)

@cli.command()
@click.pass_obj
def orders(exchange: AbstractExchange):
    """Fetch open limit orders for the selected exchange."""
    result = exchange.orders()
    click.echo(result)
    
@cli.command()
@click.argument("symbol", default='')
@click.pass_obj
def positions(exchange: AbstractExchange, symbol):
    """
    Returns all active positions of <symbol>
    if <symbol> is empty return all active positions
    """
    res = exchange.positions(symbol)
    click.echo(res)


@cli.command()
@click.pass_obj
def balance(exchange: AbstractExchange):
    """Fetch Account Balance"""
    result = exchange.balance()
    click.echo("Account Balance:")
    click.echo(result)

@cli.command()
@click.argument("side")
@click.argument("symbol")
@click.argument("price")
@click.argument("size")
@click.argument("leverage")
@click.pass_obj
def limit(exchange: AbstractExchange, side: str, symbol: str, price: float, size: float, leverage: int):
    """Create a limit order on the selected exchange."""
    result = exchange.limit(side, symbol.upper(), price, size, leverage)
    click.echo(result)

@cli.command()
@click.argument("side")
@click.argument("symbol")
@click.argument("price")
@click.argument("size")
@click.argument("leverage")
@click.argument("orders")
@click.argument("spread")
@click.pass_obj
def limit_batch(exchange: AbstractExchange, side: str, symbol: str, price: float, size: float, leverage: int, orders: int, spread: float):
    """Create a group of limit orders based on a 1:1 ratio on the selected exchange."""
    result = exchange.limit_batch(side, symbol.upper(), price, size, leverage, orders, spread)
    click.echo(result)

@cli.command()
@click.argument("side")
@click.argument("symbol")
@click.argument("price")
@click.argument("size")
@click.argument("leverage")
@click.argument("orders")
@click.argument("spread")
@click.pass_obj
def limit_x3(exchange: AbstractExchange, side: str, symbol: str, price: float, size: float, leverage: int, orders: int, spread: float):
    """Create a batch of limit orders based on a 3:2:1 ratio on the exchange."""
    order, summary = exchange.limit_x3(side, symbol.upper(), price, size, leverage, orders, spread)
    click.echo("Order Details:")
    click.echo(order)
    click.echo("\nSummary:")
    click.echo(summary)

@cli.command()
@click.argument("symbol")
@click.pass_obj
def nuke(exchange : AbstractExchange, symbol: str):
    "Force Cancels all orders on market given <symbol>"
    result = exchange.cancel_all(symbol.upper())
    click.echo(result)


@cli.command()
@click.pass_obj
def nuke_all(exchange: AbstractExchange):
    """Force Cancels all open/active orders on market"""
    result = exchange.nuke_all()
    click.echo(result)

if __name__ == "__main__":
    cli()