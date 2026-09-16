# V1.8
"""
Kalshi BTC 15-min UP/DOWN martingale bot.

Run:
    python bot.py --config config.yaml

Each 15-minute cycle:
  1. Check the just-closed window's settlement result.
  2. If we had a pending bet on that window, score it (win/loss) and update
     the martingale stake accordingly.
  3. Decide UP or DOWN for the NEW window based on strategy.mode.
  4. Wait until the configured entry window (e.g. minute 1-3 of the period).
  5. Find the new window's market and place a limit order at the configured
     price, sized per the current martingale stake.
  6. Persist state and sleep until the next cycle.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import time
import uuid
from typing import Optional

import yaml

try:
    import colorama
    from colorama import Fore, Style
    _COLOR_AVAILABLE = True
except ImportError:
    _COLOR_AVAILABLE = False

from kalshi_client import KalshiClient, KalshiAPIError
from state import StateStore, PendingBet
from strategy import (
    Window,
    current_window,
    previous_window,
    find_market_for_window,
    decide_side,
    current_price_cents,
    price_from_orderbook,
    count_alternations,
    get_price_series,
    decide_price_trend_side,
    get_strike_price,
    decide_spot_lean_side,
    decide_late_fade_side,
    both_sides_too_expensive,
    compute_recovery_size,
    compute_smart_hedge_count,
    compute_take_profit_profit,
    check_momentum_filter,
    get_quote,
    label,
)
import spot_price

log = logging.getLogger("bot")


def fmt_secs(seconds: float) -> str:
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    return f"{m}m{s:02d}s"


def fmt_local(utc_dt: dt.datetime) -> str:
    """Format a UTC datetime for display in the system's local timezone."""
    local_dt = utc_dt.astimezone()  # no tz arg = convert to local system timezone
    return local_dt.strftime("%H:%M:%S")


def render_table(title: str, rows: list) -> str:
    """
    Renders a simple ASCII table in HORIZONTAL orientation - one row of column
    headers, one row of values below it - as a single multi-line string for
    one log.info() call (one timestamp prefix, one color-wrap). Plain ASCII
    borders only (no box-drawing unicode) - matches the rest of the bot's
    logging, since non-ASCII characters have already caused real crashes on
    Windows consoles/configs.
    """
    headers = [h for h, _ in rows]
    values = [str(v) for _, v in rows]
    col_widths = [max(len(h), len(v)) for h, v in zip(headers, values)]

    header_line = " | ".join(h.ljust(w) for h, w in zip(headers, col_widths))
    value_line = " | ".join(v.ljust(w) for v, w in zip(values, col_widths))
    total_width = max(len(header_line), len(value_line)) + 2

    top = f" {title} ".center(total_width, "-")
    bottom = "-" * total_width
    return "\n".join([top, f" {header_line}", f" {value_line}", bottom])


def load_config(path: str) -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)


class ColorFormatter(logging.Formatter):
    """
    Colors console output by log level (WARNING=yellow, ERROR/CRITICAL=red),
    plus a few special-cased INFO messages that matter most at a glance:
    orders placed (blue), wins (green), losses (red), session-net summaries
    (yellow), chop-detected skips (magenta), and the new-window banner
    (orange). No-op (plain text) if colorama isn't installed.
    """
    LEVEL_COLORS = {}
    ORANGE = "\033[38;5;208m"  # not in colorama's basic 8-color palette, so raw ANSI 256-color code
    if _COLOR_AVAILABLE:
        LEVEL_COLORS = {
            logging.WARNING: Fore.YELLOW,
            logging.ERROR: Fore.RED,
            logging.CRITICAL: Fore.RED + Style.BRIGHT,
        }

    def format(self, record):
        formatted = super().format(record)
        if not _COLOR_AVAILABLE:
            return formatted

        color = self.LEVEL_COLORS.get(record.levelno)
        if color is None:
            msg = record.getMessage()
            if " ORDER PLACED " in msg:
                color = Fore.BLUE
            elif " WIN " in msg:
                color = Fore.GREEN + Style.BRIGHT
            elif " LOSS " in msg:
                color = Fore.RED
            elif " SESSION NET " in msg:
                color = Fore.YELLOW
            elif "CHOP DETECTED" in msg:
                color = Fore.MAGENTA
            elif msg.startswith("======== New window"):
                color = self.ORANGE

        if color:
            return f"{color}{formatted}{Style.RESET_ALL}"
        return formatted


def setup_logging(log_file: str):
    # Force UTF-8 so any non-ASCII characters in log messages don't crash on
    # platforms whose default console/file encoding is something narrower
    # (e.g. Windows' cp1252).
    if _COLOR_AVAILABLE:
        colorama.init()  # translates ANSI codes for Windows consoles that don't support them natively

    stream_handler = logging.StreamHandler(sys.stdout)
    try:
        stream_handler.stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass  # stream doesn't support reconfigure (older Python/non-standard stream) - best effort

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    stream_handler.setFormatter(ColorFormatter(fmt))

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(fmt))  # plain text in the log file, no ANSI codes

    logging.basicConfig(
        level=logging.INFO,
        handlers=[stream_handler, file_handler],
    )

    if not _COLOR_AVAILABLE:
        log.info("(Tip: run 'pip install colorama' for colored log output in the terminal.)")


def contracts_for_stake(stake: float, sizing_mode: str, price_cents: int) -> int:
    if sizing_mode in ("contracts", "recovery", "dalembert", "dalembert_reverse", "anti_martingale"):
        # "recovery" only reaches here on a fresh start / after a win (no cumulative
        # loss to chase yet), where stake is just base_size contracts - same as
        # "contracts" mode. Mid-streak recovery sizing is computed separately via
        # compute_recovery_size() and passed in as an explicit count_override instead.
        # "dalembert", "dalembert_reverse", and "anti_martingale" all track their own
        # contract count directly in current_stake (see StateStore.record_dalembert_result /
        # record_dalembert_reverse_result / record_anti_martingale_result) - no separate
        # sizing math needed here. Classic martingale ("contracts") also tracks its own
        # contract count directly in current_stake regardless of variant ("multiplier" or
        # "plus" - see StateStore.record_result) - same story, no separate math here.
        return max(1, int(round(stake)))
    if sizing_mode == "dollars":
        price_dollars = price_cents / 100.0
        if price_dollars <= 0:
            return 1
        return max(1, int(stake // price_dollars))
    raise ValueError(f"Unknown sizing mode: {sizing_mode}")


def score_pending_bets(store: StateStore, cfg: dict, ticker: str, result: Optional[str]):
    """
    Scores every pending bet that belongs to the settled `ticker`. Normally
    there's exactly one; spot_lean hedging can leave two (opposite sides of
    the same window) - since only one side can win, this naturally scores one
    as a WIN and the other as a LOSS.

    Per-bet win/loss counts and log lines always reflect each individual bet.
    But the sizing-state update (martingale stake / recovery cumulative_loss)
    is applied ONCE per session using the NET combined outcome when
    strategy.hedge.net_session_sizing is enabled (default on) and
    more than one bet was placed - otherwise a single losing bet whose hedge
    happened to win would incorrectly reset to "fresh start" even though the
    session as a whole lost money. With the toggle off, each bet updates the
    sizing state independently and immediately (the old, naive behavior).
    """
    if result is None:
        return  # not settled yet; caller already logged the wait

    matching = [b for b in store.state.pending_bets if b["ticker"] == ticker]
    if not matching:
        return
    non_matching = [b for b in store.state.pending_bets if b["ticker"] != ticker]
    if non_matching:
        log.warning(
            "%s pending bet(s) belong to a different ticker than the settlement just checked (%s) - leaving them as-is.",
            len(non_matching), ticker,
        )

    fee_cents = cfg["sizing"].get("fee_per_contract_cents", 0)
    sizing_mode = cfg["sizing"]["mode"]
    net_session_sizing = cfg["strategy"].get("hedge", {}).get("net_session_sizing", True)
    martingale_variant = cfg["sizing"].get("martingale_variant", "multiplier")
    martingale_unit = cfg["sizing"].get("martingale_unit", 1)

    outcomes = []  # (pending, won, cost_cents, pnl_cents)
    for pending in matching:
        won = result == pending["side"]
        price_cents = pending["price_cents"]
        stake = pending["stake"]
        cost_cents = int(round(stake * (price_cents + fee_cents)))  # what we actually paid, incl. fees
        pnl_cents = int(round(stake * 100)) - cost_cents if won else -cost_cents

        store.state.total_pnl_cents += pnl_cents
        if won:
            store.state.total_wins += 1
        else:
            store.state.total_losses += 1

        outcomes.append((pending, won, cost_cents, pnl_cents))

    # How much cash was tied up at the deepest point of this session: whatever's
    # already accumulated from consecutive prior LOSING sessions
    # (current_loss_streak_cost_cents - tracked for every sizing.mode; see
    # state.py) plus the combined cost of every bet placed THIS session (a
    # main + hedge bet are both outstanding simultaneously until settlement,
    # so their costs stack, not replace each other). Computed once per
    # session, not per individual bet.
    #
    # NOTE: this used to add store.state.cumulative_loss_cents here instead
    # of current_loss_streak_cost_cents. cumulative_loss_cents only exists
    # for sizing.mode="recovery" and stays 0 for every other mode, so for
    # "contracts"/"dollars"/"dalembert"/"dalembert_reverse"/"anti_martingale"
    # drawdown_now_cents collapsed to just session_total_cost_cents - i.e.
    # Max Drawdown only ever reflected the single biggest bet's own cost,
    # never the accumulated cost of a losing streak. Fixed in V1.8.
    session_total_cost_cents = sum(cost for _, _, cost, _ in outcomes)
    session_net_pnl_cents = sum(pnl for _, _, _, pnl in outcomes)
    session_won = session_net_pnl_cents >= 0

    drawdown_now_cents = store.state.current_loss_streak_cost_cents + session_total_cost_cents
    store.update_max_drawdown(drawdown_now_cents)
    store.update_loss_streak_cost(session_total_cost_cents, session_won)

    use_net_session = net_session_sizing and len(outcomes) > 1

    if use_net_session:
        net_pnl_cents = session_net_pnl_cents
        session_won = net_pnl_cents >= 0
        if sizing_mode == "recovery":
            store.record_recovery_result(
                net_pnl_cents=net_pnl_cents,
                max_cumulative_loss_cents=cfg.get("recovery", {}).get("max_cumulative_loss_cents", 2000),
            )
            # Reflect the ACTUAL resulting debt, not just whether this session alone was
            # profitable - a profitable session can still leave a deficit if it didn't
            # cover what was already owed (this was the bug: it previously reset to
            # "fresh start" any time net_pnl_cents >= 0, even if that wasn't enough to
            # clear a larger pre-existing cumulative_loss).
            next_state_str = (
                "cumulative_loss=$0.00 (fresh start)" if store.state.cumulative_loss_cents == 0
                else f"cumulative_loss=${store.state.cumulative_loss_cents / 100:.2f}"
            )
        elif sizing_mode == "dalembert":
            store.record_dalembert_result(
                won=session_won,
                unit=cfg["sizing"]["base_size"],
                max_stake=cfg["sizing"]["max_stake"],
            )
            next_state_str = f"next_stake={store.state.current_stake}"
        elif sizing_mode == "dalembert_reverse":
            dr_cfg = cfg["sizing"].get("dalembert_reverse", {})
            store.record_dalembert_reverse_result(
                won=session_won,
                net_pnl_cents=net_pnl_cents,
                unit=cfg["sizing"]["dalembert_unit"],
                max_stake=cfg["sizing"]["max_stake"],
                profit_lock_cents=dr_cfg.get("profit_lock_cents", 0),
                loss_floor_cents=dr_cfg.get("loss_floor_cents", 0),
            )
            next_state_str = f"next_stake={store.state.current_stake}"
        elif sizing_mode == "anti_martingale":
            am_cfg = cfg["sizing"].get("anti_martingale", {})
            store.record_anti_martingale_result(
                won=session_won,
                variant=am_cfg.get("variant", "plus"),
                unit=am_cfg.get("unit", 1),
                multiplier=am_cfg.get("multiplier", 2),
                max_stake=cfg["sizing"]["max_stake"],
                max_steps=cfg["sizing"].get("max_anti_martingale_steps", 10),
            )
            next_state_str = f"next_stake={store.state.current_stake}"
        else:
            store.record_result(
                won=session_won,
                multiplier=cfg["sizing"]["martingale_multiplier"],
                max_steps=cfg["sizing"]["max_martingale_steps"],
                max_stake=cfg["sizing"]["max_stake"],
                variant=martingale_variant,
                unit=martingale_unit,
            )
            next_state_str = f"next_stake={store.state.current_stake}"
    else:
        # Single bet this session, or the toggle is off: each bet updates sizing state
        # immediately and independently (net_pnl_cents == pnl_cents for a single bet is
        # exactly equivalent to the old cost-based behavior).
        for _, won, _, pnl_cents in outcomes:
            if sizing_mode == "recovery":
                store.record_recovery_result(
                    net_pnl_cents=pnl_cents,
                    max_cumulative_loss_cents=cfg.get("recovery", {}).get("max_cumulative_loss_cents", 2000),
                )
            elif sizing_mode == "dalembert":
                store.record_dalembert_result(
                    won=won,
                    unit=cfg["sizing"]["base_size"],
                    max_stake=cfg["sizing"]["max_stake"],
                )
            elif sizing_mode == "dalembert_reverse":
                dr_cfg = cfg["sizing"].get("dalembert_reverse", {})
                store.record_dalembert_reverse_result(
                    won=won,
                    net_pnl_cents=pnl_cents,
                    unit=cfg["sizing"]["dalembert_unit"],
                    max_stake=cfg["sizing"]["max_stake"],
                    profit_lock_cents=dr_cfg.get("profit_lock_cents", 0),
                    loss_floor_cents=dr_cfg.get("loss_floor_cents", 0),
                )
            elif sizing_mode == "anti_martingale":
                am_cfg = cfg["sizing"].get("anti_martingale", {})
                store.record_anti_martingale_result(
                    won=won,
                    variant=am_cfg.get("variant", "plus"),
                    unit=am_cfg.get("unit", 1),
                    multiplier=am_cfg.get("multiplier", 2),
                    max_stake=cfg["sizing"]["max_stake"],
                    max_steps=cfg["sizing"].get("max_anti_martingale_steps", 10),
                )
            else:
                store.record_result(
                    won=won,
                    multiplier=cfg["sizing"]["martingale_multiplier"],
                    max_steps=cfg["sizing"]["max_martingale_steps"],
                    max_stake=cfg["sizing"]["max_stake"],
                    variant=martingale_variant,
                    unit=martingale_unit,
                )
        if sizing_mode == "recovery":
            next_state_str = (
                "cumulative_loss=$0.00 (fresh start)" if store.state.cumulative_loss_cents == 0
                else f"cumulative_loss=${store.state.cumulative_loss_cents / 100:.2f}"
            )
        else:
            next_state_str = f"next_stake={store.state.current_stake}"

    for pending, won, cost_cents, pnl_cents in outcomes:
        outcome = "WIN" if won else "LOSS"
        if len(outcomes) == 1:
            # Common case (no hedge): include the sizing state in this same table.
            log.info(render_table(outcome, [
                ("Ticker", ticker),
                ("Bet", label(pending["side"])),
                ("Actual", label(result)),
                ("Count", str(pending["stake"])),
                ("Price", f"{pending['price_cents']}c (incl. {fee_cents}c fee)"),
                ("Cost", f"${cost_cents / 100:.2f}"),
                ("Record", f"{store.state.total_wins}-{store.state.total_losses}"),
                ("State", next_state_str),
                ("Total PnL", f"${store.state.total_pnl_cents / 100.0:.2f}"),
                ("Max Drawdown", f"${store.state.max_drawdown_cents / 100.0:.2f}"),
            ]))
        else:
            # Multiple bets this session (hedge fired): each bet's own result here;
            # the combined session outcome + sizing state is shown separately below.
            log.info(render_table(outcome, [
                ("Ticker", ticker),
                ("Bet", label(pending["side"])),
                ("Actual", label(result)),
                ("Count", str(pending["stake"])),
                ("Price", f"{pending['price_cents']}c (incl. {fee_cents}c fee)"),
                ("Cost", f"${cost_cents / 100:.2f}"),
                ("Record", f"{store.state.total_wins}-{store.state.total_losses}"),
            ]))

    if len(outcomes) > 1:
        net_pnl_cents = sum(pnl for _, _, _, pnl in outcomes)
        log.info(render_table("SESSION NET", [
            ("Bets", str(len(outcomes))),
            ("Net PnL", f"${net_pnl_cents / 100:+.2f}"),
            ("Result", "WIN" if net_pnl_cents >= 0 else "LOSS"),
            ("State", next_state_str),
            ("Total PnL", f"${store.state.total_pnl_cents / 100.0:.2f}"),
            ("Max Drawdown", f"${store.state.max_drawdown_cents / 100.0:.2f}"),
        ]))

    store.state.pending_bets = non_matching
    store.save()


def compute_settlement_from_strikes(
    client: KalshiClient, series_ticker: str, prev_window: Window, current_window: Window,
    poll_interval: int, timeout_sec: float,
):
    """
    Determines the previous window's UP/DOWN result WITHOUT waiting for
    Kalshi's own settlement to be published. Since these 15-min windows are
    back-to-back (window N's close_time is exactly window N+1's open_time),
    and floor_strike is the BTC price at the moment a window opens, the
    previous window's closing price is effectively the current window's
    floor_strike - so the result can be read the instant the NEW window's
    market appears (floor_strike is set immediately on creation), instead of
    waiting for the old window to fully close and Kalshi to compute/publish
    an official result, which is what wait_for_settlement (the old approach)
    had to do.

        result = 'yes' (UP)   if current_window's floor_strike > previous window's floor_strike
        result = 'no'  (DOWN) if current_window's floor_strike < previous window's floor_strike

    Returns (prev_market_or_None, result_or_None) - same shape the caller
    previously got from wait_for_settlement, so this is a drop-in replacement.
    """
    start = time.monotonic()
    last_status_logged = None
    while True:
        prev_market = find_market_for_window(client, series_ticker, prev_window)
        current_market = find_market_for_window(client, series_ticker, current_window)
        prev_strike = get_strike_price(prev_market) if prev_market else None
        current_strike = get_strike_price(current_market) if current_market else None
        prev_ticker = prev_market.get("ticker") if prev_market else "unknown"

        if prev_strike is not None and current_strike is not None:
            if current_strike > prev_strike:
                result = "yes"
            elif current_strike < prev_strike:
                result = "no"
            else:
                log.warning(
                    "Settlement from strikes: ticker=%s prev_strike and new_strike are exactly equal "
                    "($%s) - genuinely ambiguous, proceeding with result=UNKNOWN.",
                    prev_ticker, f"{prev_strike:,.2f}",
                )
                return prev_market, None

            log.info(
                "Settlement from strikes: ticker=%s prev_strike=$%s new_strike=$%s -> result=%s",
                prev_ticker, f"{prev_strike:,.2f}", f"{current_strike:,.2f}", label(result),
            )
            return prev_market, result

        status_key = (prev_strike is not None, current_strike is not None)
        elapsed = time.monotonic() - start
        if status_key != last_status_logged:
            missing = []
            if prev_strike is None:
                missing.append("previous window's floor_strike")
            if current_strike is None:
                missing.append("current window's floor_strike")
            log.info("Waiting for settlement: %s not available yet", " and ".join(missing))
            last_status_logged = status_key

        if elapsed > timeout_sec:
            log.warning(
                "Gave up computing settlement from strikes for ticker=%s after %s - proceeding with result=UNKNOWN.",
                prev_ticker, fmt_secs(elapsed),
            )
            return prev_market, None

        time.sleep(poll_interval)


def wait_and_place_bet(client: KalshiClient, store: StateStore, cfg: dict, window: Window, side):
    """
    Poll the live ask price for `side` throughout the entry window.

    `side` can be a fixed 'yes'/'no' string, OR a zero-arg callable that gets
    invoked fresh on every poll and returns 'yes'/'no'/None - used by the
    spot_lean strategy, where direction is decided live from the current BTC
    spot price rather than fixed once at window-open. If the callable returns
    None (e.g. spot price exactly at target, or a fetch failed), that poll is
    treated as "no signal yet" and retried next interval.

    Normal sizing (or recovery mode with no loss to chase yet): waits for
    price <= max_price_cents, then bets a fixed contract count.

    Recovery mode, mid-loss-streak: instead of a fixed price ceiling, computes
    the contract count needed AT THE CURRENT LIVE PRICE to fully recover the
    accumulated loss (+ fees, + a minimum profit). Places the bet as soon as
    that computed count is within the safety cap; otherwise keeps waiting for
    a better price. If the entry window closes with no qualifying bet found
    either way, skips - the loss (if any) carries over to the next window.
    """
    series_ticker = cfg["market"]["series_ticker"]
    max_price = cfg["strategy"]["max_price_cents"]
    min_price = cfg["strategy"].get("min_price_cents", 1)
    entry_end = cfg["strategy"]["entry_end_min"]

    mf_cfg = cfg["strategy"].get("momentum_filter", {})
    mf_enabled = mf_cfg.get("enabled", False)
    mf_lookback = mf_cfg.get("lookback_sec", 30)
    mf_price_history = []  # rolling (monotonic_ts, price) samples, trimmed to mf_lookback each poll

    dynamic_side = callable(side)
    # spot_lean and late_fade both check live BTC price every poll, so they
    # get their own (tighter, by default) interval - separate from the
    # general poll_interval_sec used by every other strategy mode and by
    # market/order-book polling elsewhere. Read from whichever of the two
    # dynamic-side strategy blocks is actually configured as the mode, not
    # always spot_lean's - otherwise late_fade would silently inherit
    # spot_lean's poll_interval_sec instead of falling back to the runtime
    # default. The momentum filter also wants frequent sampling to build a
    # meaningful short rolling window, so it uses the same tighter interval
    # when active.
    _configured_mode = cfg["strategy"].get("mode")
    _dynamic_cfg = cfg["strategy"].get(_configured_mode, {}) if _configured_mode in ("spot_lean", "late_fade") else {}
    poll_interval = (
        _dynamic_cfg.get("poll_interval_sec", cfg["runtime"]["poll_interval_sec"])
        if (dynamic_side or mf_enabled) else cfg["runtime"]["poll_interval_sec"]
    )
    resolve_side = side if dynamic_side else (lambda: side)
    opening_side_label = "DYNAMIC (follows live BTC price)" if dynamic_side else label(side)

    recovering = cfg["sizing"]["mode"] == "recovery" and store.state.cumulative_loss_cents > 0
    if recovering:
        fee = cfg["sizing"].get("fee_per_contract_cents", 0)
        rc = cfg.get("recovery", {})
        min_profit = rc.get("min_profit_cents", 1)
        max_contracts = rc.get("max_contracts", 50)
        recovery_max_price = rc.get("max_price_cents", max_price)
        recovery_min_price = rc.get("min_price_cents", min_price)
        log.info(
            "Entry window open - target=%s, RECOVERY MODE: chasing $%.2f cumulative loss "
            "(need count*(100-price-%sc fee) >= %sc, cap %s contracts, price range %sc-%sc) - "
            "checking every %ss, %s left in entry window",
            opening_side_label, store.state.cumulative_loss_cents / 100, fee,
            store.state.cumulative_loss_cents + min_profit, max_contracts, recovery_min_price, recovery_max_price,
            poll_interval,
            fmt_secs(entry_end * 60 - (dt.datetime.now(dt.timezone.utc) - window.open_time).total_seconds()),
        )
    else:
        log.info(
            "Entry window open - target=%s, watching for price in %sc-%sc (checking every %ss, %s left in entry window)",
            opening_side_label, min_price, max_price, poll_interval,
            fmt_secs(entry_end * 60 - (dt.datetime.now(dt.timezone.utc) - window.open_time).total_seconds()),
        )

    while True:
        now = dt.datetime.now(dt.timezone.utc)
        minutes_into_window = (now - window.open_time).total_seconds() / 60.0
        time_left_entry = entry_end * 60 - minutes_into_window * 60
        if minutes_into_window > entry_end:
            log.info(
                "Entry window closed (minute %.1f > %.1f) without a qualifying price - skipping bet, target=%s.",
                minutes_into_window, entry_end, opening_side_label,
            )
            return

        market = find_market_for_window(client, series_ticker, window)
        if not market:
            log.info("Ticker for this window not listed yet - retrying in %ss...", poll_interval)
            time.sleep(poll_interval)
            continue

        ticker = market["ticker"]

        side = resolve_side()
        if side is None:
            log.info(
                "No directional signal yet (spot price at/near target, or spot price fetch failed) - "
                "retrying in %ss...", poll_interval,
            )
            time.sleep(poll_interval)
            continue

        mf_allowed, mf_direction = True, None
        if mf_enabled:
            mf_spot, _mf_source = spot_price.get_btc_spot_price()
            if mf_spot is not None:
                now_mono = time.monotonic()
                mf_price_history.append((now_mono, mf_spot))
                cutoff = now_mono - mf_lookback
                mf_price_history[:] = [(t, p) for t, p in mf_price_history if t >= cutoff]
            mf_allowed, mf_direction = check_momentum_filter(mf_price_history, mf_lookback, side)

        price = None
        try:
            orderbook = client.get_orderbook(ticker)
            price = price_from_orderbook(orderbook, side)
        except KalshiAPIError as e:
            log.debug("Orderbook fetch failed for %s: %s", ticker, e)

        source = "orderbook"
        if price is None:
            # Orderbook empty (fresh/thin market) - fall back to market summary quote.
            price = current_price_cents(market, side)
            source = "market-summary"

        if price is None:
            log.info("ticker=%s target=%s - no price data yet (empty book), retrying in %ss...",
                      ticker, label(side), poll_interval)
            time.sleep(poll_interval)
            continue

        if mf_enabled and not mf_allowed:
            log.info(
                "Momentum filter: last %ss BTC trend is %s, blocking a %s order - waiting | entry closes in %s",
                mf_lookback, label(mf_direction), label(side), fmt_secs(time_left_entry),
            )
            time.sleep(poll_interval)
            continue

        if recovering and price > recovery_max_price:
            log.info(
                "ticker=%s target=%-4s price=%sc exceeds recovery price ceiling %sc - waiting for a "
                "better price rather than chase a thin-margin fill [%s] | entry closes in %s",
                ticker, label(side), price, recovery_max_price, source, fmt_secs(time_left_entry),
            )
        elif recovering and price < recovery_min_price:
            log.info(
                "ticker=%s target=%-4s price=%sc is below recovery price floor %sc - too cheap, usually means "
                "the market disagrees with this side - waiting [%s] | entry closes in %s",
                ticker, label(side), price, recovery_min_price, source, fmt_secs(time_left_entry),
            )
        elif recovering:
            required_count = compute_recovery_size(
                store.state.cumulative_loss_cents, price, fee, min_profit, max_contracts,
            )
            met = required_count is not None
            if met:
                log.info(
                    "ticker=%s target=%-4s price=%sc -> %s contract(s) recovers $%.2f loss + $%.2f profit [%s] "
                    "(MET) | entry closes in %s",
                    ticker, label(side), price, required_count, store.state.cumulative_loss_cents / 100,
                    min_profit / 100, source, fmt_secs(time_left_entry),
                )
                filled = _submit_order(client, store, cfg, window, ticker, side, price, count_override=required_count)
                if filled > 0:
                    return
                log.info("Recovery order did not fill - continuing to watch for a qualifying price.")
            else:
                log.info(
                    "ticker=%s target=%-4s price=%sc -> would need too many contracts to recover "
                    "(over %s cap) [%s], waiting | entry closes in %s",
                    ticker, label(side), price, max_contracts, source, fmt_secs(time_left_entry),
                )
        else:
            met = min_price <= price <= max_price
            log.info(
                "ticker=%s target=%-4s price=%sc | entry closes in %s",
                ticker, label(side), price, fmt_secs(time_left_entry),
            )
            if met:
                filled = _submit_order(client, store, cfg, window, ticker, side, price)
                if filled > 0:
                    return
                log.info("Order did not fill - continuing to watch for a qualifying price.")

        time.sleep(poll_interval)


def _current_both_side_prices(client: KalshiClient, ticker: str, market: dict) -> tuple:
    """
    Returns (up_price_cents, down_price_cents) - the live ask price for each
    side, preferring the orderbook (freshest) and falling back to the
    market-summary quote if the book is thin/empty, same precedence
    wait_and_place_bet uses for a single side. Either value may be None if
    no price data is available yet. Used only by the "late_fade" strategy's
    entry gate, which (unlike everything else in this file) needs BOTH
    sides' prices at once rather than just the one side a strategy picked.
    """
    orderbook = None
    try:
        orderbook = client.get_orderbook(ticker)
    except KalshiAPIError as e:
        log.debug("Orderbook fetch failed for %s: %s", ticker, e)

    prices = {}
    for side in ("yes", "no"):
        price = price_from_orderbook(orderbook, side) if orderbook else None
        if price is None:
            price = current_price_cents(market, side)
        prices[side] = price
    return prices["yes"], prices["no"]


def _submit_order(
    client: KalshiClient, store: StateStore, cfg: dict, window: Window, ticker: str, side: str,
    price_cents: int, count_override: Optional[int] = None,
) -> int:
    """
    Places an order and returns the ACTUAL number of contracts filled (0 if
    the order didn't fill at all). With time_in_force="immediate_or_cancel",
    Kalshi can accept an order (returning a valid order_id, no error) but
    fill zero contracts if the price moved or there was no matching liquidity
    at that instant - so a successful API call does NOT by itself mean a real
    position was opened. Callers MUST check the return value: only treat the
    bet as placed (and stop polling for this window) if it's > 0; otherwise
    keep looking, the same as if the price hadn't qualified at all.
    """
    if count_override is not None:
        count = count_override
        stake = count  # for recovery mode, "stake" recorded on the pending bet is simply the contract count
    else:
        stake = store.state.current_stake
        count = contracts_for_stake(stake, cfg["sizing"]["mode"], price_cents)

    fee = cfg["sizing"].get("fee_per_contract_cents", 0)
    client_order_id = str(uuid.uuid4())
    cost = count * (price_cents + fee) / 100.0
    payout = count * 1.0

    log.info(render_table("ORDER PLACED", [
        ("Ticker", ticker),
        ("Target", label(side)),
        ("Count", str(count)),
        ("Price", f"{price_cents}c (+{fee}c fee)"),
        ("Cost", f"${cost:.2f}"),
        ("Potential Payout", f"${payout:.2f}"),
        ("Dry Run", str(cfg["runtime"]["dry_run"])),
    ]))

    order_id = None
    filled_count = count  # dry_run: no real fill data, simulate a full fill (existing behavior)
    if not cfg["runtime"]["dry_run"]:
        try:

            market = client.get_market(ticker)
            exchange_index = market.get("exchange_index")

            resp = client.create_order(
                ticker=ticker,
                side=side,
                count=count,
                price_cents=price_cents,
                client_order_id=client_order_id,
                exchange_index=exchange_index,
            )
            order_id = resp.get("order_id")
            fill_count_raw = resp.get("fill_count")
            log.info(
                "Order response: order_id=%s fill_count=%s remaining_count=%s avg_fill_price=%s",
                order_id, fill_count_raw, resp.get("remaining_count"), resp.get("average_fill_price"),
            )
            try:
                filled_count = int(round(float(fill_count_raw))) if fill_count_raw is not None else 0
            except (TypeError, ValueError):
                filled_count = 0
        except KalshiAPIError as e:
            log.error("Order placement failed: %s", e)
            return 0

    if filled_count <= 0:
        log.warning(
            "Order for %s contract(s) on %s (%s) did NOT fill (fill_count=0) - Kalshi accepted the order but "
            "no contracts were actually bought. NOT tracking this as a bet; will keep looking.",
            count, ticker, label(side),
        )
        return 0

    if filled_count < count:
        log.warning(
            "Order for %s contract(s) on %s (%s) only PARTIALLY filled: %s contract(s) actually bought. "
            "Tracking the bet at the real filled count, not the originally requested count.",
            count, ticker, label(side), filled_count,
        )

    store.add_pending_bet(
        PendingBet(
            window_close_iso=window.close_time.isoformat(),
            ticker=ticker,
            side=side,
            stake=filled_count,
            price_cents=price_cents,
            order_id=order_id,
        )
    )
    return filled_count


def _session_side_totals(store: StateStore, ticker: str, fee_cents: int) -> dict:
    """
    Sums up contract count and total cost (incl. fees), per side, across all
    pending bets already placed for this ticker's session (main bet + any
    hedges so far). Used by smart hedge sizing to know the current exposure
    on each side before deciding the next hedge's size.
    """
    totals = {"yes": [0, 0], "no": [0, 0]}  # [count, cost_cents]
    for b in store.state.pending_bets:
        if b["ticker"] != ticker:
            continue
        side = b["side"]
        count = b["stake"]
        cost = int(round(count * (b["price_cents"] + fee_cents)))
        totals[side][0] += count
        totals[side][1] += cost
    return {"yes": tuple(totals["yes"]), "no": tuple(totals["no"])}


def monitor_hedge(
    client: KalshiClient, store: StateStore, cfg: dict, window: Window,
    target_price: float, initial_side: str, count: int,
):
    """
    Runs after the primary bet is placed for ANY strategy.mode - hedging and
    take-profit only care about live BTC spot vs. this window's own target
    (floor_strike), not about which strategy chose the initial side. Only
    active if strategy.hedge.enabled and/or strategy.take_profit.enabled.
    Keeps watching the live BTC spot price for the rest of this 15-min
    window and can take TWO opposite kinds of action:

    HEDGE (protects against a loss): if spot crosses back to the OPPOSITE
    side of the window's target (by hedge.threshold_pct), places an opposing
    bet - up to hedge.max_hedges_per_window times (default 1). Both bets get
    scored independently against the window's actual settlement (see
    score_pending_bets) - one will win, one will lose.

    Sizing (hedge.smart_sizing):
      false (default) - the hedge uses the SAME contract count as the bet
        that started this session. Simple, but since a session can cross
        back and forth several times, the side that ultimately wins is not
        guaranteed to cover the cost of the bets placed on the other side.
      true - each hedge is sized to EQUALIZE total contracts on both sides
        (main + hedges so far), which fixes the payout to the same amount
        regardless of which side wins - and only fires once that guaranteed
        outcome clears hedge.min_profit_cents, waiting for a better price
        otherwise (up until the window closes).

    TAKE-PROFIT (the mirror of hedge - locks in a gain): fires only when ALL
    THREE hold at once: (1) session time is within
    [take_profit.time_start_min, take_profit.time_end_min] minutes into the
    window, (2) the OPPOSITE side's live price is within
    [take_profit.min_price_cents, take_profit.max_price_cents], and (3) the
    live spot-vs-target gap still favors the main bet's side by at least
    take_profit.threshold_pct. When all three hold AND buying the opposite
    side at its current price (same contract count as the main bet) would
    lock in a guaranteed profit >= take_profit.min_profit_cents no matter
    which side the window ultimately settles on, it does so - after which
    monitoring stops for this window, since the outcome no longer affects
    this session's P&L.
    """
    sl_cfg = cfg["strategy"].get("spot_lean", {})
    # hedge/take-profit monitoring runs "underneath" whichever strategy placed
    # the main bet, so its own poll interval isn't tied to any one strategy's
    # config block - but it still defaults to a dynamic-side strategy's own
    # (tighter) interval when the ACTIVE mode is one of those, same reasoning
    # as wait_and_place_bet's poll_interval above, rather than always
    # spot_lean's regardless of what's actually configured.
    _configured_mode = cfg["strategy"].get("mode")
    _hedge_poll_cfg = cfg["strategy"].get(_configured_mode, {}) if _configured_mode in ("spot_lean", "late_fade") else sl_cfg
    hedge_cfg = cfg["strategy"].get("hedge", {})
    tp_cfg = cfg["strategy"].get("take_profit", {})
    hedge_enabled = hedge_cfg.get("enabled", False)
    tp_enabled = tp_cfg.get("enabled", False)

    if not hedge_enabled and not tp_enabled:
        return
    if target_price is None:
        log.debug("Hedge/take-profit monitor: no target price available, skipping watch for this window.")
        return

    hedge_fresh_start_only = hedge_cfg.get("fresh_start_only", True)
    if hedge_enabled and hedge_fresh_start_only and count != 1:
        log.info(
            "Hedge monitor: main order count=%s (not a fresh start) - hedging is disabled for this session "
            "per hedge.fresh_start_only (hedges only follow a single-contract, fresh-start main bet).",
            count,
        )
        hedge_enabled = False

    tp_fresh_start_only = tp_cfg.get("fresh_start_only", True)
    if tp_enabled and tp_fresh_start_only and count != 1:
        log.info(
            "Take-profit monitor: main order count=%s (not a fresh start) - take-profit is disabled for this "
            "session per take_profit.fresh_start_only.",
            count,
        )
        tp_enabled = False

    if not hedge_enabled and not tp_enabled:
        return

    max_price = cfg["strategy"]["max_price_cents"]
    min_price = cfg["strategy"].get("min_price_cents", 1)
    poll_interval = _hedge_poll_cfg.get("poll_interval_sec", cfg["runtime"]["poll_interval_sec"])
    series_ticker = cfg["market"]["series_ticker"]
    safety_margin = dt.timedelta(seconds=poll_interval)
    fee_cents = cfg["sizing"].get("fee_per_contract_cents", 0)

    # --- Hedge params ---
    hedge_threshold = hedge_cfg.get("threshold_pct", 0.0)
    max_hedges = hedge_cfg.get("max_hedges_per_window", 1)
    smart_sizing = hedge_cfg.get("smart_sizing", False)
    smart_min_profit = hedge_cfg.get("min_profit_cents", 1)
    smart_max_contracts = hedge_cfg.get("max_contracts", 50)

    current_side = initial_side
    hedges_placed = 0
    chasing_side = None  # the side we're currently trying to get a qualifying price for, or None

    # --- Take-profit params ---
    tp_threshold_pct = tp_cfg.get("threshold_pct", 0.0)
    tp_min_profit = tp_cfg.get("min_profit_cents", 1)
    tp_min_price = tp_cfg.get("min_price_cents", 1)
    tp_max_price = tp_cfg.get("max_price_cents", max_price)
    tp_time_start = tp_cfg.get("time_start_min", 0)
    tp_time_end = tp_cfg.get("time_end_min", 15)

    if hedge_enabled:
        log.info(
            "Hedge monitor ON for the rest of this window - target=$%s. If BTC crosses back over this, "
            "will place a %s-contract opposite order (up to %s hedge(s), price %sc-%sc).",
            f"{target_price:,.2f}", count, max_hedges, min_price, max_price,
        )
    if tp_enabled:
        log.info(
            "Take-profit monitor ON for minute %s-%s of this window - target=$%s. If the opposite side's price "
            "(range %sc-%sc) would lock in >= $%.2f profit while spot is still favoring %s by >= %.3f%%, will "
            "place a %s-contract opposite order to lock it in.",
            tp_time_start, tp_time_end, f"{target_price:,.2f}", tp_min_price, tp_max_price,
            tp_min_profit / 100, label(initial_side), tp_threshold_pct, count,
        )

    while (hedge_enabled and hedges_placed < max_hedges) or tp_enabled:
        now = dt.datetime.now(dt.timezone.utc)
        if now >= window.close_time - safety_margin:
            log.info("Hedge/take-profit monitor: window is closing, stopping watch for this cycle.")
            return

        # Re-checked on EVERY iteration, including while chasing a qualifying price below -
        # this is the fix: previously, once a crossing was detected, the bot stopped
        # watching spot price entirely while waiting for the contract price to qualify,
        # so a reversal back before that happened went unnoticed.
        spot, spot_source = spot_price.get_btc_spot_price()

        if tp_enabled:
            minutes_into_window = (now - window.open_time).total_seconds() / 60.0
            if tp_time_start <= minutes_into_window <= tp_time_end:
                tp_side, tp_gap_pct = decide_spot_lean_side(spot, target_price, tp_threshold_pct)
                if tp_side == initial_side:
                    opposite_side = "no" if initial_side == "yes" else "yes"
                    tp_market = find_market_for_window(client, series_ticker, window)
                    if tp_market:
                        tp_ticker = tp_market["ticker"]
                        tp_price = None
                        try:
                            tp_orderbook = client.get_orderbook(tp_ticker)
                            tp_price = price_from_orderbook(tp_orderbook, opposite_side)
                        except KalshiAPIError:
                            pass
                        if tp_price is None:
                            tp_price = current_price_cents(tp_market, opposite_side)

                        if tp_price is not None and tp_min_price <= tp_price <= tp_max_price:
                            totals = _session_side_totals(store, tp_ticker, fee_cents)
                            main_count_now, main_cost_now = totals[initial_side]
                            if main_count_now > 0:
                                guaranteed_profit = compute_take_profit_profit(
                                    count, main_cost_now, tp_price, fee_cents,
                                )
                                log.info(
                                    "Take-profit check: ticker=%s minute=%.1f spot gap=%+.3f%% (need >=%.3f%%) "
                                    "opposite=%s price=%sc (range %sc-%sc) -> locking in now would guarantee "
                                    "$%.2f (need >=$%.2f)",
                                    tp_ticker, minutes_into_window, tp_gap_pct, tp_threshold_pct,
                                    label(opposite_side), tp_price, tp_min_price, tp_max_price,
                                    guaranteed_profit / 100, tp_min_profit / 100,
                                )
                                if guaranteed_profit >= tp_min_profit:
                                    log.info(
                                        "Take-profit: PLACING TAKE-PROFIT ORDER - ticker=%s opposite=%s count=%s "
                                        "price=%sc",
                                        tp_ticker, label(opposite_side), count, tp_price,
                                    )
                                    filled = _submit_order(
                                        client, store, cfg, window, tp_ticker, opposite_side, tp_price,
                                        count_override=count,
                                    )
                                    if filled > 0:
                                        log.info(
                                            "Take-profit order filled - profit locked in for this session; "
                                            "stopping hedge/take-profit monitoring for this window.",
                                        )
                                        return
                                    log.info("Take-profit order did not fill - continuing to watch.")

        if not hedge_enabled:
            time.sleep(poll_interval)
            continue

        side, gap_pct = decide_spot_lean_side(spot, target_price, hedge_threshold)

        if side is None or side == current_side:
            if chasing_side is not None:
                log.info(
                    "Hedge monitor: BTC reverted back to %s before a qualifying price was found for %s - "
                    "cancelling that hedge attempt.",
                    label(current_side), label(chasing_side),
                )
                chasing_side = None
            time.sleep(poll_interval)
            continue

        if side != chasing_side:
            # Either a fresh crossing, or the crossing flipped to yet another side.
            log.info(
                "Hedge monitor: BTC spot from %s=$%s target=$%s gap=%+.3f%% - "
                "was %s, now %s. Watching for a qualifying price to place the hedge order...",
                spot_source, f"{spot:,.2f}", f"{target_price:,.2f}", gap_pct, label(current_side), label(side),
            )
            chasing_side = side

        market = find_market_for_window(client, series_ticker, window)
        if not market:
            time.sleep(poll_interval)
            continue
        hedge_ticker = market["ticker"]
        price = None
        try:
            orderbook = client.get_orderbook(hedge_ticker)
            price = price_from_orderbook(orderbook, side)
        except KalshiAPIError:
            pass
        if price is None:
            price = current_price_cents(market, side)

        if price is None:
            log.info("Hedge monitor: ticker=%s target=%s - no price data yet, waiting...", hedge_ticker, label(side))
            time.sleep(poll_interval)
            continue

        if not (min_price <= price <= max_price):
            log.info(
                "Hedge monitor: ticker=%s target=%s price=%sc outside range %sc-%sc, waiting...",
                hedge_ticker, label(side), price, min_price, max_price,
            )
            time.sleep(poll_interval)
            continue

        if smart_sizing:
            totals = _session_side_totals(store, hedge_ticker, fee_cents)
            this_count, this_cost = totals[side]
            other_side = "no" if side == "yes" else "yes"
            other_count, other_cost = totals[other_side]

            hedge_count, guaranteed_profit = compute_smart_hedge_count(
                this_count, this_cost, other_cost, price, fee_cents, smart_min_profit, smart_max_contracts,
            )
            if hedge_count is None:
                log.info(
                    "Hedge monitor: %s's existing %s contract(s) already cover(s) %s's %s contract(s) with "
                    "the target profit even without a new hedge (or price/size no longer viable) - skipping "
                    "this crossing.",
                    label(side), this_count, label(other_side), other_count,
                )
                current_side = side
                chasing_side = None
                time.sleep(poll_interval)
                continue

            log.info(
                "Hedge monitor: ticker=%s target=%s price=%sc -> %s contract(s) needed so a %s win covers "
                "everything spent on %s ($%.2f total so far) plus profit - guarantees $%.2f if %s wins",
                hedge_ticker, label(side), price, hedge_count, label(side), label(other_side),
                other_cost / 100, guaranteed_profit / 100, label(side),
            )
            log.info(
                "Hedge monitor: PLACING SMART HEDGE ORDER - ticker=%s target=%s count=%s price=%sc",
                hedge_ticker, label(side), hedge_count, price,
            )
            filled = _submit_order(client, store, cfg, window, hedge_ticker, side, price, count_override=hedge_count)
        else:
            log.info("Hedge monitor: ticker=%s target=%s price=%sc (range=%sc-%sc, MET)",
                      hedge_ticker, label(side), price, min_price, max_price)
            log.info(
                "Hedge monitor: PLACING HEDGE ORDER - ticker=%s target=%s count=%s price=%sc",
                hedge_ticker, label(side), count, price,
            )
            filled = _submit_order(client, store, cfg, window, hedge_ticker, side, price, count_override=count)

        if filled > 0:
            hedges_placed += 1
            current_side = side
            chasing_side = None
        else:
            log.info("Hedge order did not fill - continuing to watch for a qualifying price.")

        time.sleep(poll_interval)


def detect_environment(base_url: str) -> str:
    if "demo-api.kalshi.co" in base_url:
        return "DEMO (paper trading - prices will NOT match kalshi.com)"
    if "kalshi.com" in base_url or "kalshi.co" in base_url:
        return "PRODUCTION (real money, real prices)"
    return f"UNKNOWN ({base_url})"


def run(cfg: dict):
    setup_logging(cfg["runtime"]["log_file"])
    env = detect_environment(cfg["kalshi"]["base_url"])
    client = KalshiClient(
        key_id=cfg["kalshi"]["key_id"],
        private_key_path=cfg["kalshi"]["private_key_path"],
        base_url=cfg["kalshi"]["base_url"],
    )
    store = StateStore(cfg["runtime"]["state_file"], base_stake=cfg["sizing"]["base_size"])

    # --- LIVE TICK LOGGER (permanent, strategy-independent) ---
    # Config-gated via live_tick.enabled (default: on). `file`/`interval_sec`
    # fall back to the old runtime.live_tick_file / a 1s default if the new
    # live_tick: block isn't present, so existing configs keep working.
    lt_cfg = cfg.get("live_tick", {})
    tick_logger = None
    if lt_cfg.get("enabled", True):
        from data_logger import LiveTickLogger
        tick_logger = LiveTickLogger(
            client=client,
            series_ticker=cfg["market"]["series_ticker"],
            output_file=lt_cfg.get("file", cfg["runtime"].get("live_tick_file", "live_ticks.jsonl")),
            interval_sec=lt_cfg.get("interval_sec", 1.0),
            new_file_per_session=lt_cfg.get("new_file_per_session", True),
        )
        tick_logger.start()
    else:
        log.info("Live tick logger disabled (live_tick.enabled: false in config).")
    # -----------------------------------------------------------

    log.info("=" * 60)
    log.info("Environment : %s", env)
    log.info("Base URL    : %s", cfg["kalshi"]["base_url"])
    log.info("Dry run     : %s", cfg["runtime"]["dry_run"])
    log.info("Series      : %s", cfg["market"]["series_ticker"])
    log.info("Strategy    : mode=%s price_range=%sc-%sc entry=%s-%s min",
              cfg["strategy"]["mode"], cfg["strategy"].get("min_price_cents", 1), cfg["strategy"]["max_price_cents"],
              cfg["strategy"]["entry_start_min"], cfg["strategy"]["entry_end_min"])
    log.info("=" * 60)
    last_processed_window_open = None

    try:
        while True:
            now = dt.datetime.now(dt.timezone.utc)
            window = current_window(now)

            if window.open_time != last_processed_window_open:
                log.info(
                    "======== New window %s -> %s ========",
                    fmt_local(window.open_time), fmt_local(window.close_time),
                )

                # Determine the previous window's result by comparing floor_strike values
                # (see compute_settlement_from_strikes) instead of waiting for Kalshi's own
                # settlement pipeline - this is available the instant the new window's
                # market appears, not after the old window fully closes and settles.
                # This single fetch serves both: scoring our pending bet (if any) and
                # deciding this window's direction, since both concern window N-1.
                prev = previous_window(window)
                series_ticker = cfg["market"]["series_ticker"]
                timeout = cfg["runtime"].get("result_wait_timeout_sec", cfg["strategy"]["entry_end_min"] * 60)
                prev_market, prev_result = compute_settlement_from_strikes(
                    client, series_ticker, prev, window, cfg["runtime"]["poll_interval_sec"], timeout,
                )
                prev_ticker = prev_market.get("ticker") if prev_market else "unknown"

                score_pending_bets(store, cfg, prev_ticker, prev_result)

                if prev_result is None:
                    log.warning(
                        "Could not determine previous window's result (ticker=%s) - skipping this window's bet.",
                        prev_ticker,
                    )
                else:
                    store.record_window_result(prev_result)

                    configured_mode = cfg["strategy"]["mode"]
                    price_trend_pct = None

                    if configured_mode == "price_trend":
                        pt_cfg = cfg["strategy"].get("price_trend", {})
                        lookback_cycles = pt_cfg.get("lookback_cycles", 6)
                        threshold_pct = pt_cfg.get("threshold_pct", 0.15)

                        series = get_price_series(client, series_ticker, window, lookback_cycles)
                        series_str = " -> ".join(f"${p:,.0f}" if p is not None else "?" for _, p in series)
                        log.info("BTC price, last %s cycles (oldest->newest): %s", lookback_cycles, series_str)

                        side, price_trend_pct = decide_price_trend_side(series, threshold_pct)
                        if side is None:
                            if price_trend_pct is None:
                                log.warning("Price-trend: insufficient price data - skipping this window's bet.")
                            else:
                                log.info(
                                    "Price-trend: change=%+.3f%% is within +/-%.3f%% threshold - no clear trend, skipping.",
                                    price_trend_pct, threshold_pct,
                                )
                        else:
                            log.info(
                                "Price-trend: change=%+.3f%% (threshold +/-%.3f%%) -> target %s",
                                price_trend_pct, threshold_pct, label(side),
                            )
                        effective_mode = "price_trend"

                    elif configured_mode == "spot_lean":
                        sl_cfg = cfg["strategy"].get("spot_lean", {})
                        sl_threshold_pct = sl_cfg.get("threshold_pct", 0.0)

                        current_market = find_market_for_window(client, series_ticker, window)
                        target_price_cache = [get_strike_price(current_market)]
                        if target_price_cache[0] is None:
                            log.info("Spot-lean: target price (floor_strike) not available yet - will keep checking.")
                        else:
                            log.info("Spot-lean: this window's target price (floor_strike) = $%s", f"{target_price_cache[0]:,.2f}")

                        def _spot_lean_side_provider(_cache=target_price_cache, _threshold=sl_threshold_pct):
                            if _cache[0] is None:
                                m = find_market_for_window(client, series_ticker, window)
                                _cache[0] = get_strike_price(m) if m else None
                                if _cache[0] is None:
                                    return None
                                log.info("Spot-lean: target price (floor_strike) now available = $%s", f"{_cache[0]:,.2f}")

                            spot, spot_source = spot_price.get_btc_spot_price()
                            chosen, gap_pct = decide_spot_lean_side(spot, _cache[0], _threshold)
                            if spot is not None and gap_pct is not None:
                                log.info(
                                    "Spot-lean: BTC spot from %s=$%s target=$%s gap=%+.3f%% -> %s",
                                    spot_source, f"{spot:,.2f}", f"{_cache[0]:,.2f}", gap_pct,
                                    label(chosen) if chosen else "no signal (too close to call)",
                                )
                            return chosen

                        side = _spot_lean_side_provider  # a callable - wait_and_place_bet resolves it live, every poll
                        effective_mode = "spot_lean"

                    elif configured_mode == "late_fade":
                        lf_cfg = cfg["strategy"].get("late_fade", {})
                        lf_threshold_pct = lf_cfg.get("threshold_pct", 0.0)

                        current_market = find_market_for_window(client, series_ticker, window)
                        target_price_cache = [get_strike_price(current_market)]
                        if target_price_cache[0] is None:
                            log.info("Late-fade: target price (floor_strike) not available yet - will keep checking.")
                        else:
                            log.info("Late-fade: this window's target price (floor_strike) = $%s", f"{target_price_cache[0]:,.2f}")

                        def _late_fade_side_provider(_cache=target_price_cache, _threshold=lf_threshold_pct):
                            if _cache[0] is None:
                                m = find_market_for_window(client, series_ticker, window)
                                _cache[0] = get_strike_price(m) if m else None
                                if _cache[0] is None:
                                    return None
                                log.info("Late-fade: target price (floor_strike) now available = $%s", f"{_cache[0]:,.2f}")

                            spot, spot_source = spot_price.get_btc_spot_price()
                            chosen, gap_pct = decide_late_fade_side(spot, _cache[0], _threshold)
                            if spot is not None and gap_pct is not None:
                                log.info(
                                    "Late-fade: BTC spot from %s=$%s target=$%s gap=%+.3f%% -> %s (anticipating reversal)",
                                    spot_source, f"{spot:,.2f}", f"{_cache[0]:,.2f}", gap_pct,
                                    label(chosen) if chosen else "no signal (too close to call)",
                                )
                            return chosen

                        side = _late_fade_side_provider  # a callable - wait_and_place_bet resolves it live, every poll
                        effective_mode = "late_fade"

                    elif configured_mode == "adaptive":
                        if store.state.last_bet_won is None:
                            effective_mode = cfg["strategy"].get("adaptive_default_mode", "momentum")
                            log.info("Adaptive mode: no prior bet yet -> starting with %s", effective_mode)
                        else:
                            effective_mode = "momentum" if store.state.last_bet_won else "reversal"
                            log.info(
                                "Adaptive mode: last bet %s -> using %s this window",
                                "WIN" if store.state.last_bet_won else "LOSS", effective_mode,
                            )
                        side = decide_side(prev_result, effective_mode)

                    else:
                        effective_mode = configured_mode
                        side = decide_side(prev_result, effective_mode)

                    minutes_into_window = (now - window.open_time).total_seconds() / 60.0
                    entry_start = cfg["strategy"]["entry_start_min"]
                    entry_end = cfg["strategy"]["entry_end_min"]
                    side_display = (
                        f"DYNAMIC ({configured_mode.replace('_', '-')})" if callable(side) else label(side)
                    )

                    if side is None:
                        # price_trend with no clear trend, or insufficient price data - already logged above.
                        pass
                    else:
                        log.info(
                            "Strategy mode=%s -> target this window: %s | stake=%s (%s) | "
                            "entry window: minute %.1f-%.1f",
                            configured_mode if configured_mode != "adaptive" else f"adaptive->{effective_mode}",
                            side_display, store.state.current_stake,
                            cfg["sizing"]["mode"], entry_start, entry_end,
                        )

                        chop_cfg = cfg["strategy"].get("chop_filter", {})
                        recent_history = ", ".join(label(r) for r in store.state.recent_results[-8:])
                        log.info("Recent window history (oldest->newest): %s", recent_history or "(none yet)")

                        skip_for_chop = False
                        if chop_cfg.get("enabled"):
                            lookback = chop_cfg.get("lookback", 4)
                            max_alt = chop_cfg.get("max_alternations", 3)
                            window_slice = store.state.recent_results[-lookback:]
                            alternations = count_alternations(window_slice)
                            log.info(
                                "Chop filter: last %s results -> %s alternation(s) (skip threshold: %s)",
                                len(window_slice), alternations, max_alt,
                            )
                            if len(window_slice) >= lookback and alternations >= max_alt:
                                skip_for_chop = True
                                store.state.skipped_chop += 1
                                store.save()
                                log.warning(
                                    "CHOP DETECTED - market whipsawing (%s alternations in last %s windows). "
                                    "Skipping this bet, target=%s. (total chop-skips: %s)",
                                    alternations, len(window_slice), side_display, store.state.skipped_chop,
                                )

                        if skip_for_chop:
                            pass  # already logged; do not enter the entry-window wait/bet flow
                        else:
                            last_heartbeat = 0.0
                            while minutes_into_window < entry_start:
                                now = dt.datetime.now(dt.timezone.utc)
                                minutes_into_window = (now - window.open_time).total_seconds() / 60.0
                                if time.monotonic() - last_heartbeat > 30:
                                    log.info(
                                        "Waiting for entry window to open in %s (target=%s)...",
                                        fmt_secs(entry_start * 60 - minutes_into_window * 60), side_display,
                                    )
                                    last_heartbeat = time.monotonic()
                                time.sleep(cfg["runtime"]["poll_interval_sec"])

                            # Late-fade's one-shot session-skip gate: checked exactly once, right as
                            # the entry window opens (not at window-open, when prices haven't had a
                            # chance to move yet). If neither side is cheap enough for a bet even at
                            # this first moment, no price drop is being anticipated for the rest of
                            # the window, so the whole session is skipped outright instead of being
                            # watched all the way to entry_end_min for something that (per this rule)
                            # isn't expected to happen.
                            skip_for_late_fade_gate = False
                            if configured_mode == "late_fade":
                                gate_market = find_market_for_window(client, series_ticker, window)
                                if gate_market:
                                    gate_ticker = gate_market["ticker"]
                                    up_price, down_price = _current_both_side_prices(client, gate_ticker, gate_market)
                                    max_price_cents = cfg["strategy"]["max_price_cents"]
                                    log.info(
                                        "Late-fade entry gate: minute %.1f - UP=%s DOWN=%s (skip session if BOTH > %sc)",
                                        minutes_into_window,
                                        f"{up_price}c" if up_price is not None else "?",
                                        f"{down_price}c" if down_price is not None else "?",
                                        max_price_cents,
                                    )
                                    if both_sides_too_expensive(up_price, down_price, max_price_cents):
                                        skip_for_late_fade_gate = True
                                        log.warning(
                                            "Late-fade: both UP (%sc) and DOWN (%sc) exceed max_price_cents=%sc "
                                            "right at entry_start_min=%s - skipping this session entirely, target=%s.",
                                            up_price, down_price, max_price_cents, entry_start, side_display,
                                        )
                                else:
                                    log.info(
                                        "Late-fade entry gate: ticker for this window not listed yet - "
                                        "proceeding without the gate check.",
                                    )

                            if skip_for_late_fade_gate:
                                pass  # already logged above
                            elif minutes_into_window <= entry_end:
                                pending_before = list(store.state.pending_bets)
                                wait_and_place_bet(client, store, cfg, window, side)
                                newly_placed = [b for b in store.state.pending_bets if b not in pending_before]
                                hedge_or_tp_enabled = (
                                    cfg["strategy"].get("hedge", {}).get("enabled", False)
                                    or cfg["strategy"].get("take_profit", {}).get("enabled", False)
                                )
                                if newly_placed and hedge_or_tp_enabled:
                                    placed = newly_placed[-1]
                                    if configured_mode in ("spot_lean", "late_fade"):
                                        hedge_target_price = target_price_cache[0]
                                    else:
                                        hedge_market = find_market_for_window(client, series_ticker, window)
                                        hedge_target_price = get_strike_price(hedge_market) if hedge_market else None
                                    monitor_hedge(
                                        client, store, cfg, window,
                                        hedge_target_price, placed["side"], int(round(placed["stake"])),
                                    )
                            else:
                                log.warning("Missed entry window (now at minute %.1f) - skipping this bet.", minutes_into_window)

                last_processed_window_open = window.open_time

            time.sleep(cfg["runtime"]["poll_interval_sec"])
    except KeyboardInterrupt:
        if tick_logger is not None:
            log.info("Stopping live tick logger...")
            tick_logger.stop()
        raise  # Re-raise so main() can catch it and exit cleanly

def main():
    parser = argparse.ArgumentParser(description="Kalshi BTC 15-min martingale bot")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    cfg = load_config(args.config)
    try:
        run(cfg)
    except KeyboardInterrupt:
        log.info("Stopped by user.")


if __name__ == "__main__":
    main()
