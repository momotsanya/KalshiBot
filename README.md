# Kalshi BTC 15-Min Bot

Watches Kalshi's `KXBTC15M` BTC up/down market, checks who won the last
15-minute window, and bets on the new window (same side = momentum, opposite
side = reversal - configurable). Uses a martingale sizing scheme with a hard
loss-streak cap.

## 1. Setup

```bash
pip install -r requirements.txt
```

You need, from your Kalshi account's API settings page:
- **Key ID**
- **Private key file** (`.pem`/`.key` - downloaded once when you create the key)

Put the private key file in this folder (or point `private_key_path` at wherever
you store it) and fill in `config.yaml`:

```yaml
kalshi:
  key_id: "your-key-id"
  private_key_path: "./kalshi_private_key.pem"
  base_url: "https://demo-api.kalshi.co/trade-api/v2"   # start here
```

## 2. Test in demo mode first

`config.yaml` ships with `dry_run: true`. Run it and watch the logs for a few
cycles before touching real money:

```bash
python bot.py --config config.yaml
```

You should see it correctly identify each window, fetch the previous window's
result, and log what it *would* bet.

> **WARNING: Demo prices != real prices.** Kalshi's demo environment
> (`demo-api.kalshi.co`) is a *separate paper-trading exchange* with its own
> order book - it does not mirror kalshi.com's real prices, and is often
> thin or empty. If you switch `base_url` to demo to test order placement,
> expect the prices you see in the bot's logs to look nothing like the real
> site. `config.yaml` defaults to the **production** data endpoint
> (`api.elections.kalshi.com`) precisely so prices match what you see on
> kalshi.com, even while `dry_run: true` keeps it from placing real orders.
> The bot logs which environment it's connected to on startup - check that
> first if prices ever look wrong.

## 3. Go live (real orders)

1. Get a production Key ID + private key from kalshi.com (`base_url` is
   already set to production for market data - this step is about enabling
   *real order placement*, which needs valid production credentials).
2. Set `dry_run: false`.
4. Double-check `strategy.limit_price_cents`, `sizing.*`, and
   `strategy.entry_start_min` / `entry_end_min` are what you actually want.
5. Run it, ideally under something that keeps it alive (`tmux`, `systemd`, a
   process manager) since it's meant to run continuously.

## Config reference (`config.yaml`)

| Key | Meaning |
|---|---|
| `strategy.mode` | `"momentum"` = bet same side as last window's winner. `"reversal"` = bet the opposite. `"adaptive"` = win-stay/lose-shift between the two. `"price_trend"` = bet on multi-cycle BTC price movement. `"spot_lean"` = bet on where live BTC price currently stands vs. this window's own target. |
| `strategy.spot_lean.hedge.enabled` | Off by default. If on, after the initial `spot_lean` bet, the bot keeps watching live BTC price for the rest of the window; if it crosses back to the opposite side of the target, places an opposing bet of the same contract count (still capped by `max_price_cents`) - up to `max_hedges_per_window` times. |
| `strategy.spot_lean.hedge.net_session_sizing` | On by default. When a hedge fires, a window can end with two bets (e.g. a losing main bet + a winning hedge). This makes the martingale/recovery sizing state react to the NET combined result of the whole session rather than whichever individual bet happens to be scored last - otherwise a session that lost money overall could incorrectly reset to "fresh start". Turn off to restore the naive per-bet-immediate behavior. |

### BTC spot price sources (spot_price.py)

Used by `spot_lean` mode. Tried in order, falling back automatically if one fails:

1. **CF Benchmarks' BRTI, scraped from their public page** - reportedly the actual index Kalshi settles these markets against, so it's the most accurate source, and this way is free (no account needed). Requires:
   ```bash
   pip install playwright
   playwright install chromium
   ```
   A single headless browser tab is opened once and kept alive for the life of the bot (reused across every price check, since launching a fresh browser on every 1-second poll would take far longer than the poll interval itself). If Playwright isn't installed, this source is skipped automatically - no setup needed if you don't want it.
2. **Coinbase** (free, public, no auth)
3. **Kraken** (free, public, no auth)
4. **Binance.US** (free, public, no auth)
| `strategy.entry_start_min` / `entry_end_min` | Bot starts polling the live price at `entry_start_min` and keeps checking every `poll_interval_sec` until `entry_end_min`. |
| `strategy.max_price_cents` | The bot only places a bet once the live ask price for the chosen side drops to this level or below. If it never does before `entry_end_min`, the window is skipped - no bet, no forced fill. |
| `sizing.mode` | `"contracts"` (fixed contract count) or `"dollars"` (fixed dollar amount, contracts derived from price). |
| `sizing.base_size` | Bet size after a win / at a fresh start. |
| `sizing.martingale_multiplier` | Multiplier applied to stake after a loss (2.0 = classic martingale). |
| `sizing.max_martingale_steps` | After this many consecutive losses, stake resets to base instead of doubling again - **this is your bankroll circuit breaker.** |
| `sizing.max_stake` | Absolute ceiling on any single bet, regardless of martingale math. |

## Important notes

- **Series ticker drift.** Kalshi occasionally renames/restructures series
  tickers. If the bot logs "No market found," open a BTC 15-min market on
  kalshi.com and confirm `market.series_ticker` in the config still matches.
- **Martingale risk.** Doubling after each loss recovers prior losses on the
  next win, but a losing streak grows your bet size exponentially. On a
  roughly-50/50 market, a run of 6-7 straight losses is not rare over time -
  size `base_size` and `max_stake` with that in mind. This bot enforces
  `max_martingale_steps` as a hard stop, but the choice of how high to set it
  is yours.
- **State file (`bot_state.json`).** This is how the bot remembers your
  current martingale stake and any bet still awaiting settlement across
  restarts. Don't delete it mid-streak unless you mean to reset to base
  stake.
- **This is not investment advice** - the momentum/reversal choice, price,
  and sizing are strategy decisions for you to make and test (start in demo
  mode) before running with real funds.

## Backtesting parameters against real history

Instead of waiting days to see how a parameter combination performs live, `backtest.py` pulls real historical settlement data (via Kalshi's public `floor_strike` and `result` fields - the same data the bot itself uses) and replays every strategy across it in seconds.

```bash
python backtest.py --config config.yaml --days 14
```

This fetches up to 14 days of settled `KXBTC15M` windows (cached locally so re-runs are instant - use `--refresh` to pull fresh data), then grid-searches:
- `momentum`, `reversal`, `adaptive` (fixed strategies, no parameters to sweep)
- `price_trend` across a grid of `lookback_cycles` x `threshold_pct` (customize with `--lookback-grid` / `--threshold-grid`)

Output looks like:
```
Strategy                                                  Bets   Wins  Losses  Skipped  WinRate MaxLossStreak    PnL@50c
price_trend(lookback=6, threshold=0.15%)                   187    102      85       41    54.5%             5     $+8.50
momentum                                                    240    122     118        0    50.8%             7     $+2.00
...
```

**Read this before trusting the numbers:**
- `WinRate` is real directional accuracy from historical data. `PnL` assumes every bet filled at one flat price (`--assumed-price-cents`, default 50c) - that's illustrative for comparing strategies against each other, not a real profit estimate. A strategy needs win rate > its actual entry price (in cents, as a %) to have edge - e.g. buying at 45c needs >45% accuracy.
- Check the `Bets` column before trusting a row. A few hundred bets is a reasonable minimum before treating a win-rate difference as more than noise; tens of bets is not.
- Gaps in the historical data (API limits, downtime) are detected and excluded from lookback calculations automatically, so a "trend" is never measured across missing time.
- `MaxLossStreak` is the worst run of consecutive losses seen in the data - size `max_martingale_steps`/`max_stake` to survive it, not just whatever streak you've personally seen live.

### Realistic mode (`--realistic`) - uses real historical prices, not a flat assumption

The default mode above has a real limitation: it assumes every single window gets bet at one flat price, but the live bot only actually bets when the price drops to `max_price_cents` within your entry window - otherwise it skips. Since a cheap price on your target side often means the market itself is leaning against you, this isn't a minor detail; it can make live results look meaningfully different from the flat-price backtest.

`--realistic` fixes this by pulling real historical 1-minute price data (via Kalshi's candlestick endpoint) for every window and replaying the *exact same filter* the live bot applies - a bet only counts if the real price actually qualified within your configured `entry_start_min`/`entry_end_min`, using the real price paid for PnL:

```bash
python backtest.py --config config.yaml --days 14 --realistic
```

```
===== REALISTIC results (real historical prices, max_price_cents=50, entry window 1.0-3.0 min) =====
Strategy                                    Bets   Wins  Losses  Skipped  WinRate  AvgPrice  MaxLossStreak    RealPnL
price_trend(lookback=6, threshold=0.15%)      64     35      29       82    54.7%     44.2c              4    $+3.85
momentum                                      98     47      51       47    48.0%     41.8c              6    $-2.10
```

Notes:
- `Skipped` here means the real historical price never dropped to your threshold in time - the live bot would have skipped that window too. Expect a much higher skip count than the flat-price mode.
- First run fetches one candlestick series per historical window (can take a while for large `--days`); results are cached to `backtest_candles_cache_<series>.json`, so re-running with different strategy/parameter grids afterward is instant. Use `--refresh-candles` to force a refetch.
- This is the mode to trust for realistic win-rate and PnL - the flat-price mode above is best used first, for a fast broad look at which `lookback_cycles`/`threshold_pct` region looks promising before confirming with `--realistic`.

## Web dashboard (`webapp/`)

A local browser UI for editing `config.yaml` and monitoring/starting/stopping the bot, instead of hand-editing the file and running `bot.py` from the terminal.

```bash
pip install -r requirements.txt   # make sure flask and ruamel.yaml are installed
cd webapp
python server.py
```

This prints a password and a URL (e.g. `http://localhost:8420`) - open it in a browser on the same machine, or from your phone on the same Wi-Fi network using the LAN URL it also prints. Log in with the password shown, or set your own ahead of time with `DASHBOARD_PASSWORD=yourpassword python server.py`.

- **Monitor tab** - live P&L/record/drawdown stats (from `bot_state.json`) and a live-tailing, color-coded log view, plus Start/Stop buttons that launch/kill `bot.py` as a real subprocess. If the bot exits unexpectedly (bad credentials, crash, etc.), the dashboard shows the actual error instead of just going quiet.
- **Configuration tab** - every setting from `config.yaml`, organized the same way as this README's config reference below. Saving writes straight back to `config.yaml` using `ruamel.yaml`, which preserves all of the file's existing comments - only the values you actually changed are touched. Restart the bot for changes to take effect.

This is a local development server (Flask's built-in one) - fine for running on your own machine or home network, not meant to be exposed to the public internet.

## Files

- `bot.py` - main loop / entrypoint
- `backtest.py` - grid-search strategy parameters against real historical data
- `kalshi_client.py` - signed REST client (RSA-PSS auth)
- `strategy.py` - window timing, market lookup, UP/DOWN decision
- `spot_price.py` - live BTC/USD spot price fetcher (Coinbase/Kraken/Binance.US), used by `spot_lean` mode
- `state.py` - martingale stake persistence
- `config.yaml` - all settings
- `webapp/server.py` - Flask backend for the web dashboard (config edit + bot start/stop/monitor)
- `webapp/templates/index.html`, `webapp/static/app.js`, `webapp/static/style.css` - dashboard frontend

