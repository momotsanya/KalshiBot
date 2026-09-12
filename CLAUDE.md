<!-- V1.0 -->
# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Kalshi BTC 15-Min Bot - an automated trading bot for Kalshi's KXBTC15M (BTC up/down) market that runs on 15-minute cycles. It uses configurable strategies (momentum, reversal, adaptive, price_trend, spot_lean) with martingale position sizing and loss-streak circuit breakers. The bot can run in dry-run mode for testing or place real orders with live Kalshi credentials.

## Quick Start Commands

```bash
# Setup
pip install -r requirements.txt

# Run the bot (dry-run by default in config.yaml)
python bot.py --config config.yaml

# Backtest parameters against 14 days of real historical data
python backtest.py --config config.yaml --days 14

# Backtest with real historical prices (shows actual entry filtering + PnL)
python backtest.py --config config.yaml --days 14 --realistic

# Run the web dashboard for config editing and monitoring
cd webapp
python server.py
```

## Architecture

### Core Trading Loop (`bot.py`)

The bot runs in 15-minute cycles:

1. **Score pending bets** - Check if a bet from the last window settled, update martingale state
2. **Decide UP/DOWN** - Pick a side for the new window based on configured strategy
3. **Wait for entry window** - Sleep until `entry_start_min` (e.g., minute 6 of the window)
4. **Place order** - Within `entry_start_min` to `entry_end_min`, monitor price and place a limit order when it meets criteria
5. **Persist state** - Save martingale stake, pending bet, and P&L to `bot_state.json`
6. **Sleep until next cycle**

### Key Components

**`strategy.py`** — Window timing, market discovery, and side decisions
- `Window` dataclass: `open_time` (UTC) + `close_time` (15-min apart)
- `current_window(now)` / `previous_window()` - Window math
- `find_market_for_window()` - Search API for the ticker matching a time window
- `decide_side()` - Strategy-specific logic (momentum/reversal/adaptive/price_trend/spot_lean)
- `get_strike_price()` - Extract Kalshi's settlement target from market metadata
- `current_price_cents()` - Poll live UP/DOWN prices from the orderbook

**`kalshi_client.py`** — Signed REST API client
- RSA-PSS-SHA256 request signing required by Kalshi
- `KalshiClient.get_markets()` - Market search by series ticker + status
- `KalshiClient.get_fills()` - Retrieve filled orders and their settlement results
- `KalshiClient.place_order()` - Place limit orders (POST /portfolio/orders)
- `KalshiClient.cancel_order()` - Cancel open orders

**`state.py`** — Martingale state persistence
- `BotState` - Tracks `current_stake`, `consecutive_losses`, `total_wins`/`total_losses`, `pending_bets` (normally 0-1, up to 2 with spot_lean hedging), `cumulative_loss_cents` (recovery mode), `max_drawdown_cents`
- `StateStore` - Loads/saves state from `bot_state.json` on every cycle
- **Restart-safe** — Bot can safely be killed and restarted; martingale state survives

**`spot_price.py`** — Live BTC/USD spot price fetching (for `spot_lean` mode)
- Tries in order: CF Benchmarks (Playwright scraping), Coinbase, Kraken, Binance.US
- Used by `spot_lean` strategy to determine if live BTC price justifies a bet
- Returns fallback to `None` if all sources fail

**`data_logger.py`** / **`live_tick.py`** — Raw market data recording
- One JSON line per second (configurable `interval_sec`)
- Records: BTC spot price, Kalshi strike, live UP/DOWN prices
- Written to `./data/live_ticks_*.jsonl` (one per window by default)
- Never affects trading logic; pure analysis data

**`backtest.py`** — Grid-search strategy parameters against historical data
- Fetches real settlement results via `floor_strike` + `result` fields
- Grid-searches `price_trend` parameters, or compares fixed strategies (momentum/reversal/adaptive)
- Flat-price mode: assumes all bets filled at one price (`--assumed-price-cents`)
- `--realistic` mode: uses real historical candlestick prices, shows actual entry filtering + PnL
- Caches data locally; re-runs are instant

**`webapp/`** — Browser-based dashboard (Flask backend)
- `server.py` - Flask app; login with password, start/stop bot as subprocess, live log tail
- `templates/index.html` + `static/app.js` / `style.css` - Frontend
- Edits `config.yaml` via `ruamel.yaml` (preserves comments)
- Monitor tab shows live P&L, record, drawdown from `bot_state.json`

### Configuration (`config.yaml`)

**Top-level sections:**
- `kalshi` — `key_id`, `private_key_path` (RSA .pem file), `base_url` (demo vs. production)
- `market` — `series_ticker` (normally `"KXBTC15M"`)
- `strategy` — `mode` ("momentum" | "reversal" | "adaptive" | "price_trend" | "spot_lean"), mode-specific parameters
- `sizing` — `mode` ("contracts" | "dollars" | "recovery"), base stake, martingale multiplier, max steps
- `recovery` — Recovery mode (only when `sizing.mode: "recovery"`), cumulative loss tracking
- `live_tick` — Data logging, file path, per-window rotation
- `runtime` — `dry_run` (no real orders), `poll_interval_sec`, state file, log file

**Strategy modes:**
- **momentum** — Bet same side as last window's winner
- **reversal** — Bet opposite side from last window's winner
- **adaptive** — Win-stay / lose-shift between momentum and reversal
- **price_trend** — Examine multi-cycle BTC price direction; grid-searchable `lookback_cycles` + `threshold_pct`
- **spot_lean** — Bet based on where live BTC spot price stands vs. the window's settlement target (`floor_strike`); includes hedging logic

**Sizing modes:**
- **contracts** — Fixed contract count per bet
- **dollars** — Fixed dollar amount; bot calculates contracts from price
- **recovery** — Variant of martingale that tracks cumulative loss and scales to recover it (escape hatch vs. doubling forever)

## Important Patterns & Conventions

### State Persistence & Restarts
- `bot_state.json` is loaded at startup, updated after every cycle
- `pending_bets` is a list (normally 0-1 entries, up to 2 with spot_lean hedging) to handle multiple concurrent positions
- Never delete `bot_state.json` mid-streak unless you mean to reset martingale stake to base

### Window Timing
- Windows are `:00`, `:15`, `:30`, `:45` (UTC). E.g., 14:30-14:45 is one window.
- `entry_start_min: 6` means the bot starts polling at minute 6 of the window (14:36 if window is 14:30-14:45)
- `entry_end_min: 14` means it stops checking at minute 14 (14:44)
- If price never reaches `max_price_cents` threshold during entry window, window is skipped (no bet placed)

### Martingale Safety
- `max_martingale_steps` is a hard circuit breaker — after N consecutive losses, stake resets to base
- `max_stake` is an absolute ceiling on any bet size regardless of martingale math
- Recovery mode (`sizing.mode: "recovery"`) offers an alternative: tracks cumulative unrecovered loss and scales recovery attempts

### API Signing
- Kalshi requires RSA-PSS request signing: message = `timestamp_ms + METHOD + full_path`, signature via `RSA-PSS-SHA256(message, salt_len=digest_len)`
- **Critical:** the full path must include the base URL's path prefix (e.g., `/trade-api/v2/portfolio/orders`), not just `/portfolio/orders`
- See `kalshi_client.py` docstring for full details

### Market Discovery
- Kalshi occasionally renames series tickers (KXBTC15M has been stable, but check if bot logs "No market found")
- `strategy.find_market_for_window()` searches all market statuses (open, closed, settled) and matches by close time ±2 min tolerance
- Candidate market list is cached per window to avoid redundant API calls

## Development Workflow

### Testing Changes
1. **Dry-run mode** — Keep `runtime.dry_run: true` in config.yaml; bot will log decisions without placing orders
2. **Single cycle** — For quick feedback, modify entry/exit timing or add breakpoints in `bot.py`
3. **Backtest** — Use `python backtest.py --config config.yaml --days 7` to test strategy changes against recent data
4. **Realistic backtest** — Run with `--realistic` to see actual entry filtering and PnL before going live

### Adding a New Strategy
1. Add a new `mode` in `config.yaml` + its parameters under `strategy.<mode_name>`
2. Implement the decision logic in `strategy.py` as a new function (e.g., `decide_my_strategy_side()`)
3. Add a case in `strategy.decide_side()` to call your function
4. Test with `backtest.py --config config.yaml` before running live

### Debugging
- **Bot crashes with "No market found"** — Series ticker has changed; verify `market.series_ticker` matches the current market on kalshi.com
- **Orders not filling** — Check `max_price_cents` and actual orderbook prices in the logs; may be too restrictive
- **Martingale state looks wrong** — Review `bot_state.json` and check if pending bets settled correctly (look for API errors during settlement scoring)
- **Log rotation** — Logs go to `bot.log` by default; configure `runtime.log_file` in config.yaml

## Files & Responsibilities

| File | Purpose |
|------|---------|
| `bot.py` | Main loop: entry point, cycle orchestration, logging setup |
| `strategy.py` | Window math, market discovery, side decision logic for all modes |
| `kalshi_client.py` | REST API client with RSA-PSS signing |
| `state.py` | `BotState` dataclass + `StateStore` for persistence |
| `spot_price.py` | Live BTC/USD price fetcher (CF Benchmarks, Coinbase, Kraken, Binance.US) |
| `data_logger.py` / `live_tick.py` | 1-line-per-second raw market data recorder |
| `backtest.py` | Historical simulation + grid search of parameters |
| `config.yaml` | All runtime settings (strategy, sizing, timing, credentials) |
| `bot_state.json` | Runtime state: martingale stake, pending bets, P&L, generated on first run |
| `webapp/server.py` | Flask backend: login, start/stop bot, config UI, log tail |
| `webapp/templates/` + `webapp/static/` | Dashboard HTML/JS/CSS frontend |

## Deployment

- **Local/home network:** Run `python bot.py` directly or via the dashboard
- **Always use a process manager** (`systemd`, `supervisor`, `tmux`) to keep it alive across restarts
- **Real money:** Start in demo mode first (`base_url: demo-api.kalshi.co`), prove the strategy in dry-run, then switch to production with `dry_run: false`
- **Credentials:** Never commit `config.yaml` with real Key IDs or .pem files; use environment variable substitution or a separate secrets file (not in the repo)

