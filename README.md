# f3l

Trade KuCoin Futures from the terminal: limit, market and stop orders,
take-profit / stop-loss, ladders, iceberg and hidden orders, plus chase,
TWAP, VWAP and trailing-stop algorithms.

> **This places real, leveraged orders with real money.** Try new commands
> with `--dry-run` first, then with the smallest size the contract allows.

## Install

```bash
poetry install          # or: pip install -e .
cp .env.example .env    # then add your KuCoin API key, secret and passphrase
f3l --help
```

Market-data commands (`price`, `contract`) and every `--dry-run` work
without API keys.

## Sizes, prices and safety

- **SIZE is in USD** (notional). Add `--lots` to give a number of contracts
  instead. Sizes are rounded *down* to whole lots.
- **Prices are snapped to the tick size**: buys round down and sells round up,
  so rounding never makes an order more aggressive.
- **Leverage** is `-l N`; without it, `F3L_DEFAULT_LEVERAGE` (default 1) is used.
- **Limits** in `.env` (see `.env.example`):
  - `F3L_MAX_LEVERAGE`: orders above this are refused (default 20)
  - `F3L_MAX_ORDER_USD`: max notional per command (default 10000)
  - `F3L_CONFIRM_ABOVE_USD`: ask before sending at or above this (default 1000)
- `--dry-run` shows exactly what would be sent. `-y` skips the confirmation.
- `nuke` / `nuke-all` always ask first (unless `-y`).

## Commands

### Info

```bash
f3l price XBTUSDTM            # mark, index, last, bid/ask, spread
f3l contract ETHUSDTM         # tick size, lot size, value of one lot
f3l balance
f3l orders [SYMBOL]           # open orders + untriggered stop orders
f3l positions [SYMBOL]
```

### Basic orders

```bash
f3l limit buy XBTUSDTM 60000 500 -l 5                # $500 limit buy at 60000, 5x
f3l limit buy XBTUSDTM 60000 500 --post-only         # maker only
f3l limit sell XBTUSDTM 70000 5000 --iceberg 500     # iceberg: show $500 at a time
f3l limit sell XBTUSDTM 70000 5000 --hidden          # not shown in the order book
f3l limit buy XBTUSDTM 60000 500 --tp 65000 --sl 58000   # with take-profit / stop-loss
f3l limit buy XBTUSDTM 60000 500 --ioc               # fill what you can now, cancel the rest

f3l market sell ETHUSDTM 1000 --reduce-only

f3l stop sell XBTUSDTM 58000 500 --reduce-only       # stop-market
f3l stop buy XBTUSDTM 72000 500 --limit 72100        # stop-limit (breakout entry)

f3l tpsl XBTUSDTM --tp 70000 --sl 58000              # TP/SL on the open position
```

Stop direction is worked out automatically: a trigger above the current
price fires when price rises to it, below fires when it falls to it.
`--trigger mark|last|index` picks the price that triggers (default: mark).

### Ladders

Spread one order across several prices:

```bash
f3l ladder SIDE SYMBOL FROM TO SIZE -n ORDERS [--shape ...] [--spacing ...]
```

| Shape | Sizes (first to last) |
|---|---|
| `even` (default) | 1 : 1 : 1 : 1 |
| `linear` | 1 : 2 : 3 : 4 (the old `limit-x3`) |
| `exp --factor 1.5` | 1 : 1.5 : 2.25 : 3.4 |
| `custom --ratios 1,1,2,4` | whatever you give |

- Sizes grow from `FROM` towards `TO`. For a buy ladder, put the price nearest
  the market first so the biggest orders sit lowest. `--reverse` flips it.
- `--spacing linear` gives equal dollar gaps, `--spacing geometric` gives equal
  percentage gaps.
- `--tp` / `--sl` add one reduce-only stop each, sized to the whole ladder. If
  only part of the ladder fills, KuCoin trims the stop to your position.
- Orders go out in batches of 20. The result table shows each order's ID or
  the exchange's error.

```bash
f3l ladder buy XBTUSDTM 60000 55000 3000 -n 5 --shape linear --sl 54000 --dry-run
```

`limit-batch` and `limit-x3` were replaced by `ladder`.

### Cancelling

```bash
f3l cancel ORDER_ID
f3l nuke XBTUSDTM                 # all orders on one symbol, stop orders included
f3l nuke-all                      # everything, everywhere
f3l nuke-all --keep-stops         # leave stop / TP / SL orders alone
```

### Algorithms (run while the command is open)

KuCoin doesn't offer these natively, so `f3l` runs them itself. They only
work while the command is running. **Ctrl+C stops them cleanly:** working
orders are cancelled and a summary is printed.

```bash
# Chase: post-only order that follows the best bid/ask until filled
f3l chase buy XBTUSDTM 2000 --max-price 61000 --timeout 5m --then-market

# TWAP: equal slices over time (market orders, or IOC limits with --limit-price)
f3l twap sell ETHUSDTM 10000 -d 1h --slices 30 --limit-price 3900

# VWAP: like TWAP, but sized by typical volume for this time of day
f3l vwap buy XBTUSDTM 10000 -d 2h --lookback-days 7

# Trailing stop on an open position
f3l trail XBTUSDTM -p 2 --activation 65000
```

- **chase** pays maker fees. `--offset N` sits N ticks inside the spread.
  `--max-price` caps the chase (a floor for sells).
- **twap / vwap** send the first slice right away. Anything a slice fails to
  fill (with `--limit-price`) rolls into the next one. Durations look like
  `90s`, `15m`, `2h` or `1h30m`.
- **vwap** looks at the same time window on each of the last N days and sends
  more when the market is usually busier. With no history it falls back to TWAP.
- **trail** keeps a real close-position stop on KuCoin and moves it as price
  improves. The new stop goes in before the old one is cancelled. If the
  command exits, the last stop **stays on KuCoin** (it just stops trailing),
  unless you pass `--cancel-on-exit`.

## Development

```bash
poetry install
poetry run pytest
```

Tests use an in-memory fake exchange and a fake clock, so they never touch
KuCoin and the algorithm tests finish instantly.

Code layout (`src/f3l/`):

| File | What's in it |
|---|---|
| `cli.py` | Click commands, previews, confirmations |
| `exchange.py` | `AbstractExchange` interface and the `KuCoin` REST client |
| `models.py` | Contract rules (tick/lot rounding), order requests |
| `ladder.py` | Ladder sizing and price maths |
| `algos.py` | Chase, TWAP/VWAP scheduling, trailing stop |
| `config.py` | Safety limits from `.env` |

To add an exchange, implement `AbstractExchange` and register it in
`EXCHANGES` in `exchange.py`.

### Notes

- In cross-margin mode KuCoin sets leverage per account, not per order, so
  `-l` may not apply there.
- Market orders have no slippage cap on KuCoin. To cap the price, use
  `limit --ioc` or `--limit-price` on twap/vwap.
