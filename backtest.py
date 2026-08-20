"""
Backtest the bot's strategies against real historical KXBTC15M data.

Fetches settled markets' floor_strike (BTC price at window open) and result
(UP/DOWN) for as much history as Kalshi's API will return, then replays every
strategy mode across that history for a grid of parameter combinations -
in seconds, instead of waiting days for live samples.

Run:
    python backtest.py --config config.yaml --days 14
    python backtest.py --config config.yaml --days 14 --realistic

TWO MODES:
  - Default (flat-price): tests DIRECTIONAL ACCURACY only - assumes every
    window gets bet at one flat assumed price (--assumed-price-cents). Fast,
    good for a first broad look at lookback_cycles/threshold_pct, but does
    NOT reflect that the live bot only bets when the price actually drops to
    max_price_cents within the entry window - many windows it would skip
    entirely get counted here as if they were bet on.
  - --realistic: fetches real historical 1-minute candlesticks (yes_bid/
    yes_ask) for each window and replays the SAME price-threshold filter the
    live bot applies - only counts a bet if the real historical price
    actually qualified within your configured entry window, using the REAL
    price paid for PnL. This is slower on first run (one extra API call per
    window; cached to disk afterward) but is the mode to trust for realistic
    win-rate and PnL numbers.
  - Results are cached to local JSON files so re-running with different
    parameter grids doesn't re-fetch from the API every time. Delete the
    cache files (or pass --refresh / --refresh-candles) to pull fresh data.
"""
from __future__ import annotations

import argparse
import datetime as dt
import itertools
import json
import logging
import sys
from dataclasses import dataclass
from typing import Optional

import yaml

from kalshi_client import KalshiClient, KalshiAPIError
from strategy import decide_side, decide_price_trend_side

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("backtest")


@dataclass
class HistoricalWindow:
    ticker: str
    close_time: dt.datetime
    floor_strike: Optional[float]
    result: Optional[str]  # 'yes' or 'no'


def load_config(path: str) -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)


def parse_strike(raw) -> Optional[float]:
    if raw is None:
        return None
    try:
        if isinstance(raw, str):
            raw = raw.replace("$", "").replace(",", "")
        return float(raw)
    except (TypeError, ValueError):
        return None


def fetch_history(client: KalshiClient, series_ticker: str, days: int) -> list[HistoricalWindow]:
    now = dt.datetime.now(dt.timezone.utc)
    min_close_ts = int((now - dt.timedelta(days=days)).timestamp())

    log.info("Fetching settled %s markets from the last %s days (this may take a moment)...", series_ticker, days)
    raw_markets = client.get_markets_history(series_ticker, status="settled", min_close_ts=min_close_ts)
    log.info("Fetched %s settled markets.", len(raw_markets))

    windows = []
    for m in raw_markets:
        close_str = m.get("close_time")
        if not close_str:
            continue
        try:
            close_time = dt.datetime.fromisoformat(close_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        result = m.get("result")
        if result not in ("yes", "no"):
            continue
        strike = parse_strike(m.get("floor_strike"))
        ticker = m.get("ticker")
        if not ticker:
            continue
        windows.append(HistoricalWindow(ticker, close_time, strike, result))

    windows.sort(key=lambda w: w.close_time)
    return windows


def cache_path_for(series_ticker: str) -> str:
    return f"./backtest_cache_{series_ticker}.json"


def save_cache(windows: list[HistoricalWindow], path: str):
    data = [
        {"ticker": w.ticker, "close_time": w.close_time.isoformat(), "floor_strike": w.floor_strike, "result": w.result}
        for w in windows
    ]
    with open(path, "w") as f:
        json.dump(data, f)


def load_cache(path: str) -> Optional[list[HistoricalWindow]]:
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    windows = []
    for d in data:
        if "ticker" not in d:
            return None  # old cache format predates the ticker field - force a refetch
        windows.append(HistoricalWindow(
            d["ticker"], dt.datetime.fromisoformat(d["close_time"]), d["floor_strike"], d["result"],
        ))
    return windows


def contiguous_segments(windows: list[HistoricalWindow]) -> list[list[HistoricalWindow]]:
    """
    Split into runs of consecutive 15-min windows (no gaps). Strategies that
    look back N cycles must not span a gap (e.g. an exchange outage, or a
    period outside the fetched range), or the "trend" would be measuring
    across missing time, not real market movement.
    """
    if not windows:
        return []
    segments = [[windows[0]]]
    for prev, cur in zip(windows, windows[1:]):
        expected_gap = dt.timedelta(minutes=15)
        if cur.close_time - prev.close_time <= expected_gap + dt.timedelta(minutes=1):
            segments[-1].append(cur)
        else:
            segments.append([cur])
    return segments


# ---------- Realistic price-fill simulation (uses real historical candlesticks) ----------

def candle_cache_path_for(series_ticker: str) -> str:
    return f"./backtest_candles_cache_{series_ticker}.json"


def load_candle_cache(path: str) -> dict:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_candle_cache(cache: dict, path: str):
    with open(path, "w") as f:
        json.dump(cache, f)


def _parse_candle_price_cents(candle: dict, key: str) -> Optional[int]:
    """Handles both live ('close_dollars') and historical/archived ('close') candlestick field naming."""
    node = candle.get(key)
    if not node:
        return None
    raw = node.get("close_dollars", node.get("close"))
    if raw is None:
        return None
    try:
        if isinstance(raw, str):
            raw = raw.replace("$", "").replace(",", "")
        return int(round(float(raw) * 100))
    except (TypeError, ValueError):
        return None


def get_window_candles(
    client: KalshiClient, series_ticker: str, window: HistoricalWindow, entry_end_min: float, cache: dict,
) -> list:
    """
    Returns [(minute_offset_into_window, yes_ask_cents, no_ask_cents), ...] sorted by
    minute, covering from window open through entry_end_min (+ buffer). Cached by
    ticker so re-runs (or testing multiple strategies against the same windows)
    don't refetch.
    """
    if window.ticker in cache:
        return cache[window.ticker]

    open_time = window.close_time - dt.timedelta(minutes=15)
    start_ts = int(open_time.timestamp())
    end_ts = int((open_time + dt.timedelta(minutes=max(entry_end_min + 2, 16))).timestamp())

    try:
        raw_candles = client.get_candlesticks_auto(series_ticker, window.ticker, start_ts, end_ts, period_interval=1)
    except Exception as e:  # noqa: BLE001
        log.debug("Candlestick fetch failed for %s: %s", window.ticker, e)
        raw_candles = []

    parsed = []
    for c in raw_candles:
        end_ts_c = c.get("end_period_ts")
        if end_ts_c is None:
            continue
        end_dt = dt.datetime.fromtimestamp(end_ts_c, tz=dt.timezone.utc)
        minute_offset = (end_dt - open_time).total_seconds() / 60.0
        yes_ask = _parse_candle_price_cents(c, "yes_ask")
        yes_bid = _parse_candle_price_cents(c, "yes_bid")
        no_ask = (100 - yes_bid) if yes_bid is not None else None
        parsed.append([minute_offset, yes_ask, no_ask])
    parsed.sort(key=lambda t: t[0])

    cache[window.ticker] = parsed
    return parsed


def simulate_realistic_entry(candles: list, side: str, max_price_cents: int, entry_start_min: float, entry_end_min: float) -> Optional[int]:
    """
    Replays the SAME filter the live bot applies: scan minute-by-minute through
    the entry window, and take the price the first moment it's <= max_price_cents.
    Returns the real price (cents) paid, or None if it never qualified (i.e. the
    live bot would have skipped this window too).
    """
    for minute_offset, yes_ask, no_ask in candles:
        if minute_offset < entry_start_min or minute_offset > entry_end_min:
            continue
        price = yes_ask if side == "yes" else no_ask
        if price is not None and price <= max_price_cents:
            return price
    return None


@dataclass
class BacktestResult:
    label: str
    bets: int
    wins: int
    losses: int
    skipped: int
    max_loss_streak: int = 0
    realized_pnl_cents: Optional[int] = None   # set only by realistic (candlestick-based) runs
    avg_price_cents: Optional[float] = None    # set only by realistic runs

    @property
    def win_rate(self) -> Optional[float]:
        return (self.wins / self.bets * 100.0) if self.bets else None

    def pnl_cents(self, price_cents: int) -> Optional[int]:
        if self.bets == 0:
            return None
        return self.wins * (100 - price_cents) - self.losses * price_cents


def run_fixed_mode_backtest(segments: list[list[HistoricalWindow]], mode: str, mode_label: str) -> BacktestResult:
    """momentum / reversal: decide from the previous window's actual result."""
    bets = wins = losses = 0
    max_streak = 0
    for seg in segments:
        curr_streak = 0  # reset at each segment boundary - a gap means we don't know what happened
        for prev, cur in zip(seg, seg[1:]):
            side = decide_side(prev.result, mode)
            bets += 1
            if side == cur.result:
                wins += 1
                curr_streak = 0
            else:
                losses += 1
                curr_streak += 1
                max_streak = max(max_streak, curr_streak)
    return BacktestResult(mode_label, bets, wins, losses, 0, max_streak)


def run_adaptive_backtest(segments: list[list[HistoricalWindow]], default_mode: str = "momentum") -> BacktestResult:
    bets = wins = losses = 0
    max_streak = 0
    for seg in segments:
        last_won = None
        curr_streak = 0
        for prev, cur in zip(seg, seg[1:]):
            mode = default_mode if last_won is None else ("momentum" if last_won else "reversal")
            side = decide_side(prev.result, mode)
            bets += 1
            won = side == cur.result
            if won:
                wins += 1
                curr_streak = 0
            else:
                losses += 1
                curr_streak += 1
                max_streak = max(max_streak, curr_streak)
            last_won = won
    return BacktestResult("adaptive", bets, wins, losses, 0, max_streak)


def run_price_trend_backtest(segments: list[list[HistoricalWindow]], lookback_cycles: int, threshold_pct: float) -> BacktestResult:
    bets = wins = losses = skipped = 0
    max_streak = 0
    for seg in segments:
        if len(seg) <= lookback_cycles:
            continue
        curr_streak = 0
        for i in range(lookback_cycles, len(seg)):
            window_slice = seg[i - lookback_cycles: i + 1]
            series = [(None, w.floor_strike) for w in window_slice]
            side, _pct = decide_price_trend_side(series, threshold_pct)
            if side is None:
                skipped += 1
                continue  # a skipped window isn't a bet, so it doesn't affect the loss streak
            bets += 1
            if side == seg[i].result:
                wins += 1
                curr_streak = 0
            else:
                losses += 1
                curr_streak += 1
                max_streak = max(max_streak, curr_streak)
    label_str = f"price_trend(lookback={lookback_cycles}, threshold={threshold_pct}%)"
    return BacktestResult(label_str, bets, wins, losses, skipped, max_streak)


def run_realistic_fixed_mode_backtest(
    segments: list, mode: str, mode_label: str, client: KalshiClient, series_ticker: str,
    candle_cache: dict, max_price_cents: int, entry_start_min: float, entry_end_min: float,
) -> BacktestResult:
    bets = wins = losses = skipped = 0
    max_streak = 0
    total_pnl = 0
    total_price_paid = 0
    for seg in segments:
        curr_streak = 0
        for prev, cur in zip(seg, seg[1:]):
            side = decide_side(prev.result, mode)
            candles = get_window_candles(client, series_ticker, cur, entry_end_min, candle_cache)
            price = simulate_realistic_entry(candles, side, max_price_cents, entry_start_min, entry_end_min)
            if price is None:
                skipped += 1
                continue
            bets += 1
            total_price_paid += price
            if side == cur.result:
                wins += 1
                total_pnl += 100 - price
                curr_streak = 0
            else:
                losses += 1
                total_pnl -= price
                curr_streak += 1
                max_streak = max(max_streak, curr_streak)
    avg_price = (total_price_paid / bets) if bets else None
    return BacktestResult(mode_label, bets, wins, losses, skipped, max_streak, total_pnl, avg_price)


def run_realistic_adaptive_backtest(
    segments: list, default_mode: str, client: KalshiClient, series_ticker: str,
    candle_cache: dict, max_price_cents: int, entry_start_min: float, entry_end_min: float,
) -> BacktestResult:
    bets = wins = losses = skipped = 0
    max_streak = 0
    total_pnl = 0
    total_price_paid = 0
    for seg in segments:
        last_won = None
        curr_streak = 0
        for prev, cur in zip(seg, seg[1:]):
            mode = default_mode if last_won is None else ("momentum" if last_won else "reversal")
            side = decide_side(prev.result, mode)
            candles = get_window_candles(client, series_ticker, cur, entry_end_min, candle_cache)
            price = simulate_realistic_entry(candles, side, max_price_cents, entry_start_min, entry_end_min)
            if price is None:
                skipped += 1
                # No bet placed, so no new evidence about our own accuracy - carry the
                # adaptive state forward unchanged rather than guessing win/loss.
                continue
            bets += 1
            total_price_paid += price
            won = side == cur.result
            if won:
                wins += 1
                total_pnl += 100 - price
                curr_streak = 0
            else:
                losses += 1
                total_pnl -= price
                curr_streak += 1
                max_streak = max(max_streak, curr_streak)
            last_won = won
    avg_price = (total_price_paid / bets) if bets else None
    return BacktestResult(f"adaptive(default={default_mode})", bets, wins, losses, skipped, max_streak, total_pnl, avg_price)


def run_realistic_price_trend_backtest(
    segments: list, lookback_cycles: int, threshold_pct: float, client: KalshiClient, series_ticker: str,
    candle_cache: dict, max_price_cents: int, entry_start_min: float, entry_end_min: float,
) -> BacktestResult:
    bets = wins = losses = skipped = 0
    max_streak = 0
    total_pnl = 0
    total_price_paid = 0
    for seg in segments:
        if len(seg) <= lookback_cycles:
            continue
        curr_streak = 0
        for i in range(lookback_cycles, len(seg)):
            window_slice = seg[i - lookback_cycles: i + 1]
            series = [(None, w.floor_strike) for w in window_slice]
            side, _pct = decide_price_trend_side(series, threshold_pct)
            if side is None:
                skipped += 1
                continue

            candles = get_window_candles(client, series_ticker, seg[i], entry_end_min, candle_cache)
            price = simulate_realistic_entry(candles, side, max_price_cents, entry_start_min, entry_end_min)
            if price is None:
                skipped += 1
                continue

            bets += 1
            total_price_paid += price
            if side == seg[i].result:
                wins += 1
                total_pnl += 100 - price
                curr_streak = 0
            else:
                losses += 1
                total_pnl -= price
                curr_streak += 1
                max_streak = max(max_streak, curr_streak)
    label_str = f"price_trend(lookback={lookback_cycles}, threshold={threshold_pct}%)"
    avg_price = (total_price_paid / bets) if bets else None
    return BacktestResult(label_str, bets, wins, losses, skipped, max_streak, total_pnl, avg_price)


def print_realistic_results_table(results: list):
    header = (
        f"{'Strategy':<45} {'Bets':>6} {'Wins':>6} {'Losses':>7} {'Skipped':>8} "
        f"{'WinRate':>8} {'AvgPrice':>9} {'MaxLossStreak':>14} {'RealPnL':>10}"
    )
    print(header)
    print("-" * len(header))
    for r in sorted(results, key=lambda r: (r.realized_pnl_cents if r.realized_pnl_cents is not None else -10**9), reverse=True):
        win_rate_str = f"{r.win_rate:.1f}%" if r.win_rate is not None else "n/a"
        avg_price_str = f"{r.avg_price_cents:.1f}c" if r.avg_price_cents is not None else "n/a"
        pnl_str = f"${r.realized_pnl_cents/100:+.2f}" if r.realized_pnl_cents is not None else "n/a"
        print(
            f"{r.label:<45} {r.bets:>6} {r.wins:>6} {r.losses:>7} {r.skipped:>8} "
            f"{win_rate_str:>8} {avg_price_str:>9} {r.max_loss_streak:>14} {pnl_str:>10}"
        )


def print_results_table(results: list[BacktestResult], assumed_price_cents: int):
    header = (
        f"{'Strategy':<55} {'Bets':>6} {'Wins':>6} {'Losses':>7} {'Skipped':>8} "
        f"{'WinRate':>8} {'MaxLossStreak':>14} {'PnL@' + str(assumed_price_cents) + 'c':>10}"
    )
    print(header)
    print("-" * len(header))
    for r in sorted(results, key=lambda r: (r.win_rate or -1), reverse=True):
        win_rate_str = f"{r.win_rate:.1f}%" if r.win_rate is not None else "n/a"
        pnl = r.pnl_cents(assumed_price_cents)
        pnl_str = f"${pnl/100:+.2f}" if pnl is not None else "n/a"
        print(
            f"{r.label:<55} {r.bets:>6} {r.wins:>6} {r.losses:>7} {r.skipped:>8} "
            f"{win_rate_str:>8} {r.max_loss_streak:>14} {pnl_str:>10}"
        )


def main():
    parser = argparse.ArgumentParser(description="Backtest KXBTC15M bot strategies against historical data")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--days", type=int, default=14, help="How many days of history to pull (default 14)")
    parser.add_argument("--refresh", action="store_true", help="Ignore local cache and refetch from the API")
    parser.add_argument("--assumed-price-cents", type=int, default=50,
                         help="Flat price (cents) assumed for illustrative PnL calc (default 50)")
    parser.add_argument("--lookback-grid", default="2,3,4,5,6,8,10,12",
                         help="Comma-separated lookback_cycles values to test")
    parser.add_argument("--threshold-grid", default="0.0,0.05,0.1,0.15,0.2,0.3,0.5",
                         help="Comma-separated threshold_pct values to test")
    parser.add_argument("--realistic", action="store_true",
                         help="Also run a second pass using REAL historical order-book prices (via candlesticks) "
                              "to decide whether each window's bet would actually have been placed, and at what "
                              "price - instead of assuming a flat price on every window. Slower (one extra API "
                              "call per window on first run; cached afterward) but much closer to live behavior.")
    parser.add_argument("--refresh-candles", action="store_true",
                         help="Ignore the local candlestick cache and refetch (only relevant with --realistic)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    series_ticker = cfg["market"]["series_ticker"]
    cache_path = cache_path_for(series_ticker)

    windows = None if args.refresh else load_cache(cache_path)
    client = None
    if windows is None or args.realistic:
        client = KalshiClient(
            key_id=cfg["kalshi"]["key_id"],
            private_key_path=cfg["kalshi"]["private_key_path"],
            base_url=cfg["kalshi"]["base_url"],
        )
    if windows is None:
        try:
            windows = fetch_history(client, series_ticker, args.days)
        except KalshiAPIError as e:
            log.error("Failed to fetch historical data: %s", e)
            sys.exit(1)
        save_cache(windows, cache_path)
        log.info("Cached %s windows to %s", len(windows), cache_path)
    else:
        log.info("Loaded %s windows from cache (%s). Use --refresh to refetch.", len(windows), cache_path)

    segments = contiguous_segments(windows)
    total = sum(len(s) for s in segments)
    log.info(
        "Data spans %s windows across %s contiguous segment(s) (gaps split the data to avoid contaminating lookback windows).",
        total, len(segments),
    )
    if total < 50:
        log.warning(
            "Only %s windows of history available - results below will have wide uncertainty. "
            "Try a larger --days value if your account/API access allows it.", total,
        )

    results = []
    results.append(run_fixed_mode_backtest(segments, "momentum", "momentum"))
    results.append(run_fixed_mode_backtest(segments, "reversal", "reversal"))
    results.append(run_adaptive_backtest(segments, "momentum"))
    results.append(run_adaptive_backtest(segments, "reversal"))

    lookback_grid = [int(x) for x in args.lookback_grid.split(",")]
    threshold_grid = [float(x) for x in args.threshold_grid.split(",")]
    for lookback, threshold in itertools.product(lookback_grid, threshold_grid):
        results.append(run_price_trend_backtest(segments, lookback, threshold))

    print()
    print(f"===== Backtest results ({total} windows, {len(segments)} segment(s), assumed price {args.assumed_price_cents}c) =====")
    print_results_table(results, args.assumed_price_cents)
    print()
    print(
        "Note: WinRate is DIRECTIONAL accuracy from real history. PnL assumes every bet filled at a flat "
        f"{args.assumed_price_cents}c, which is NOT realistic - use it only to compare strategies against each "
        "other, not as a real profit estimate. A strategy needs win rate > its actual average entry price "
        "(in cents, as a %) to be profitable - e.g. buying around 45c needs >45% accuracy to have an edge."
    )
    print(
        "Also: with limited history, differences of a few percentage points between rows are often just noise - "
        "check the Bets column; anything under a few hundred bets should be treated cautiously."
    )
    print(
        "MaxLossStreak is the longest run of consecutive losses seen in this data for that strategy - useful for "
        "sizing your martingale (e.g. max_martingale_steps, max_stake): if a strategy's worst historical streak "
        "was 6, your bankroll needs to survive 6 doublings, not just the streaks you've personally seen live so far."
    )

    if args.realistic:
        max_price_cents = cfg["strategy"]["max_price_cents"]
        entry_start_min = cfg["strategy"]["entry_start_min"]
        entry_end_min = cfg["strategy"]["entry_end_min"]

        candle_cache_path = candle_cache_path_for(series_ticker)
        candle_cache = {} if args.refresh_candles else load_candle_cache(candle_cache_path)

        log.info(
            "Running realistic pass: replaying real historical prices against your config's "
            "max_price_cents=%s, entry window %s-%s min. This fetches 1-min candlesticks per window "
            "(cached afterward) - first run may take a while for %s windows.",
            max_price_cents, entry_start_min, entry_end_min, total,
        )

        realistic_results = []
        realistic_results.append(run_realistic_fixed_mode_backtest(
            segments, "momentum", "momentum", client, series_ticker, candle_cache,
            max_price_cents, entry_start_min, entry_end_min,
        ))
        realistic_results.append(run_realistic_fixed_mode_backtest(
            segments, "reversal", "reversal", client, series_ticker, candle_cache,
            max_price_cents, entry_start_min, entry_end_min,
        ))
        realistic_results.append(run_realistic_adaptive_backtest(
            segments, "momentum", client, series_ticker, candle_cache,
            max_price_cents, entry_start_min, entry_end_min,
        ))
        realistic_results.append(run_realistic_adaptive_backtest(
            segments, "reversal", client, series_ticker, candle_cache,
            max_price_cents, entry_start_min, entry_end_min,
        ))
        for lookback, threshold in itertools.product(lookback_grid, threshold_grid):
            realistic_results.append(run_realistic_price_trend_backtest(
                segments, lookback, threshold, client, series_ticker, candle_cache,
                max_price_cents, entry_start_min, entry_end_min,
            ))

        save_candle_cache(candle_cache, candle_cache_path)
        log.info("Cached candlestick data for %s tickers to %s", len(candle_cache), candle_cache_path)

        print()
        print(
            f"===== REALISTIC results (real historical prices, max_price_cents={max_price_cents}, "
            f"entry window {entry_start_min}-{entry_end_min} min) ====="
        )
        print_realistic_results_table(realistic_results)
        print()
        print(
            "This pass only counts a bet if the real historical price actually dropped to your max_price_cents "
            "threshold within your configured entry window - exactly the condition your live bot checks. "
            "Skipped means the price never qualified, same as the live bot skipping that window. "
            "RealPnL uses the actual price paid on each individual bet, not an assumed flat price."
        )


if __name__ == "__main__":
    main()
