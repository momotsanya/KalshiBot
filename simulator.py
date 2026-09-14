# V1.1
"""
Historical bot-behavior simulator for the web dashboard's "Simulator" tab.

Replays the CURRENTLY SAVED strategy/settings (config.yaml, as-is - not a
grid search) against the recorded tick files in ./data (data_logger.py's
output), producing:

  - A chronological log feed that reads exactly like a real bot.log: every
    ORDER PLACED / WIN / LOSS / SESSION NET line is built by the SAME
    render_table() calls bot.py itself uses, through bot.py's own "bot"
    logger - only the log line's timestamp is swapped from wall-clock time
    to the tick's own recorded time (see _SimLogCapture), so a replay of
    2026-08-19's data reads with 2026-08-19's timestamps.
  - A flat list of order markers (main/hedge/take_profit) for charting.
  - A pnl_series: one point per settled window with that window's resulting
    Total PnL and contract Count - this, not the raw recorded price ticks,
    is what the Simulator tab's chart plots (PnL curve + win/loss Count
    bars).
  - The resulting BotState (same shape as bot_state.json) after the replay.

This deliberately does NOT reimplement any sizing/hedge/take-profit/momentum
math - it drives tick_backtest.py's existing per-window scan functions
(_scan_spot_lean_entry, _scan_entry_fixed_side, etc.) and bot.py's own
score_pending_bets(), via the observer hooks those functions now accept, so
a simulated run's behavior is byte-for-byte the same sizing/hedge decisions
a live bot would have made against the same recorded ticks. See
tick_backtest.py's own module docstring for why replaying against your own
recorded ticks (rather than Kalshi's settlement history) is necessary for
spot_lean in the first place.

This is intentionally a SINGLE pass with ONE config (no grid, no CLI) -
tick_backtest.py remains the tool for grid-searching parameters; this module
is for "what would the bot HAVE done, with what I have configured right
now" - the dashboard's Simulator tab.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import asdict
from typing import Optional

import bot as bot_module
from bot import render_table, label

from tick_backtest import (
    load_all_windows,
    contiguous_segments,
    determine_result,
    build_spot_index,
    run_strategy_over_segments,
)

log = logging.getLogger("simulator")

SUPPORTED_MODES = ("spot_lean", "momentum", "reversal", "adaptive", "price_trend")


class _SimLogCapture(logging.Handler):
    """
    Captures bot.py's own log.info() calls - the ORDER PLACED / WIN / LOSS /
    SESSION NET tables, all built by bot.render_table() - using the
    SIMULATED tick's timestamp (self.sim_time, set by _SimObserver before
    each call that logs) instead of wall-clock time, so the replayed log
    reads exactly like a real bot.log from that point in the past.

    Mirrors logging.Formatter's own behavior for a multi-line message: the
    "{timestamp} [{level}] {name}: " prefix is attached only to the FIRST
    line of a render_table() block, with the remaining table rows/dashes
    left bare - exactly matching bot.log's real on-disk format.
    """

    def __init__(self):
        super().__init__()
        self.lines: list[str] = []
        self.sim_time: Optional[dt.datetime] = None

    def emit(self, record: logging.LogRecord):
        ts = self.sim_time or dt.datetime.now()
        ts_str = ts.strftime("%Y-%m-%d %H:%M:%S") + f",{ts.microsecond // 1000:03d}"
        message = record.getMessage()
        for i, raw_line in enumerate(message.split("\n")):
            if i == 0:
                self.lines.append(f"{ts_str} [{record.levelname}] {record.name}: {raw_line}")
            else:
                self.lines.append(raw_line)


class _SimObserver:
    """
    Passed into tick_backtest.run_strategy_over_segments(); called back at
    exactly the points a live bot.py run would log something or place an
    order. See tick_backtest.py's simulate_*_segment functions for the call
    sites.
    """

    def __init__(self, cap: _SimLogCapture, fee_cents: int, dry_run: bool):
        self.cap = cap
        self.fee_cents = fee_cents
        self.dry_run = dry_run
        self.markers: list[dict] = []
        self.pnl_points: list[dict] = []
        self.last_state: dict = {}

    def new_window(self, window):
        self.cap.sim_time = window.open_time
        bot_module.log.info(
            "======== New window %s -> %s ========",
            window.open_time.strftime("%H:%M:%S"), window.close_time.strftime("%H:%M:%S"),
        )

    def order_placed(self, window, tick_time, ticker, side, price_cents, count, kind: str):
        self.cap.sim_time = tick_time
        fee = self.fee_cents
        cost = count * (price_cents + fee) / 100.0
        payout = count * 1.0
        title = {
            "main": "ORDER PLACED",
            "hedge": "HEDGE ORDER PLACED",
            "take_profit": "TAKE-PROFIT ORDER PLACED",
        }.get(kind, "ORDER PLACED")
        bot_module.log.info(render_table(title, [
            ("Ticker", ticker),
            ("Target", label(side)),
            ("Count", str(count)),
            ("Price", f"{price_cents}c (+{fee}c fee)"),
            ("Cost", f"${cost:.2f}"),
            ("Potential Payout", f"${payout:.2f}"),
            ("Dry Run", str(self.dry_run)),
        ]))
        self.markers.append({
            "time": tick_time.isoformat(),
            "ticker": ticker,
            "side": side,
            "price_cents": price_cents,
            "count": count,
            "kind": kind,
        })

    def before_score(self, window):
        # Settlement is determined the moment the NEXT window's market
        # appears, i.e. right at this window's close - see bot.py's
        # compute_settlement_from_strikes().
        self.cap.sim_time = window.close_time

    def settled(self, window, ticker, contracts, pnl_delta_cents, total_pnl_cents):
        # One point per settled window (a whole main+hedge session counts as
        # one point, matching the combined result the SESSION NET table - or
        # the single WIN/LOSS table when there's no hedge - reports), driving
        # the Simulator tab's Total-PnL curve and Count bars. `result` here
        # is that session's own net win/loss, not each individual bet's.
        self.pnl_points.append({
            "time": window.close_time.isoformat(),
            "ticker": ticker,
            "total_pnl_cents": total_pnl_cents,
            "pnl_delta_cents": pnl_delta_cents,
            "contracts": contracts,
            "result": "win" if pnl_delta_cents >= 0 else "loss",
        })

    def segment_finished(self, state):
        # Called once per contiguous segment; the LAST call (chronologically
        # latest segment) is what ends up reflected in the result, matching
        # "state after replaying everything you've recorded".
        self.last_state = asdict(state)


def run_simulation(
    cfg: dict, data_dir: str = "./data", file_pattern: str = "*.jsonl",
    max_windows: Optional[int] = None,
) -> dict:
    """
    Runs ONE simulated pass of cfg["strategy"]["mode"] against every tick
    file found in data_dir, using cfg as-is (the same dict shape config.yaml
    loads to). Returns a dict with log_lines / markers / final_state / stats,
    or {"error": "..."} if there's nothing to replay or the mode isn't
    supported here.
    """
    windows = load_all_windows(data_dir, file_pattern)
    if not windows:
        return {"error": f"No usable *.jsonl tick files found in {data_dir}"}
    if max_windows:
        windows = windows[-max_windows:]

    strategy = cfg.get("strategy", {}).get("mode")
    if strategy not in SUPPORTED_MODES:
        return {"error": f"Strategy mode '{strategy}' isn't supported by the simulator (supported: {', '.join(SUPPORTED_MODES)})"}

    segments = contiguous_segments(windows)
    spot_index = build_spot_index(windows)
    results_by_segment = []
    for seg in segments:
        seg_results = []
        for i, w in enumerate(seg):
            next_w = seg[i + 1] if i + 1 < len(seg) else None
            seg_results.append(determine_result(w, next_w))
        results_by_segment.append(seg_results)

    cap = _SimLogCapture()
    observer = _SimObserver(
        cap,
        fee_cents=cfg.get("sizing", {}).get("fee_per_contract_cents", 0),
        dry_run=cfg.get("runtime", {}).get("dry_run", True),
    )

    # state.py logs a "starting fresh" warning every time run_strategy_over_segments
    # opens one of its per-segment temp state files (expected/harmless here - each
    # segment intentionally starts from a blank slate) - quieted the same way
    # tick_backtest.py's own __main__ already does for the grid-search tool.
    state_logger = logging.getLogger("state")
    prev_state_level = state_logger.level
    state_logger.setLevel(logging.ERROR)

    prev_level = bot_module.log.level
    bot_module.log.setLevel(logging.INFO)
    bot_module.log.addHandler(cap)
    try:
        agg = run_strategy_over_segments(strategy, segments, results_by_segment, spot_index, cfg, observer=observer)
    finally:
        bot_module.log.removeHandler(cap)
        bot_module.log.setLevel(prev_level)
        state_logger.setLevel(prev_state_level)

    total_windows = sum(len(s) for s in segments)
    return {
        "strategy": strategy,
        "windows_simulated": total_windows,
        "segments": len(segments),
        "log_lines": cap.lines,
        "markers": observer.markers,
        "pnl_series": observer.pnl_points,
        "final_state": observer.last_state,
        "stats": {
            "bets": agg.bets,
            "wins": agg.wins,
            "losses": agg.losses,
            "skipped": agg.skipped,
            "win_rate_pct": round(agg.win_rate, 2) if agg.win_rate is not None else None,
            "pnl_usd": round(agg.realized_pnl_cents / 100, 2),
            "max_drawdown_usd": round(agg.max_drawdown_cents / 100, 2),
            "max_loss_streak": agg.max_loss_streak,
            "hedges_placed": agg.hedges_placed,
            "take_profits_placed": agg.take_profits_placed,
        },
    }
