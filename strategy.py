# V1.1
"""
Time-window math, market discovery, and UP/DOWN decision logic.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Optional

from kalshi_client import KalshiClient

log = logging.getLogger("strategy")

WINDOW_MINUTES = 15


@dataclass(frozen=True)
class Window:
    open_time: dt.datetime   # UTC
    close_time: dt.datetime  # UTC


def current_window(now: Optional[dt.datetime] = None) -> Window:
    """Return the 15-min window (:00/:15/:30/:45) that `now` falls inside."""
    now = now or dt.datetime.now(dt.timezone.utc)
    floor_minute = (now.minute // WINDOW_MINUTES) * WINDOW_MINUTES
    open_time = now.replace(minute=floor_minute, second=0, microsecond=0)
    close_time = open_time + dt.timedelta(minutes=WINDOW_MINUTES)
    return Window(open_time, close_time)


def previous_window(w: Window) -> Window:
    return Window(w.open_time - dt.timedelta(minutes=WINDOW_MINUTES), w.open_time)


def _fetch_candidate_markets(client: KalshiClient, series_ticker: str) -> list[dict]:
    candidates = []
    for status in (None, "open", "settled", "closed"):
        try:
            candidates.extend(client.get_markets(series_ticker, status=status, limit=100))
        except Exception as e:  # noqa: BLE001
            log.debug("get_markets(status=%s) failed: %s", status, e)
    return candidates


def _match_market_to_window(candidates: list[dict], window: "Window") -> Optional[dict]:
    target_close = window.close_time
    best = None
    best_delta = dt.timedelta(minutes=2)  # tolerance for clock/formatting drift
    for m in candidates:
        close_str = m.get("close_time")
        if not close_str:
            continue
        try:
            m_close = dt.datetime.fromisoformat(close_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        delta = abs(m_close - target_close)
        if delta < best_delta:
            best_delta = delta
            best = m
    return best


def find_market_for_window(client: KalshiClient, series_ticker: str, window: "Window") -> Optional[dict]:
    """Find the single market whose close_time matches the given window's close_time."""
    candidates = _fetch_candidate_markets(client, series_ticker)
    return _match_market_to_window(candidates, window)


def find_markets_for_windows(client: KalshiClient, series_ticker: str, windows: list) -> dict:
    """
    Look up markets for several windows at once, fetching the candidate pool
    only ONCE and matching all windows against it (instead of one full fetch
    per window) - used by the price-trend strategy, which needs several
    consecutive windows' data per cycle.
    Returns {window: market_or_None}.
    """
    candidates = _fetch_candidate_markets(client, series_ticker)
    return {w: _match_market_to_window(candidates, w) for w in windows}


def get_strike_price(market: Optional[dict]) -> Optional[float]:
    """
    Return the BTC reference price (floor_strike) a market opened against,
    as a float dollar amount. This is set the moment the window opens, so
    it's available immediately - no need to wait for settlement.
    """
    if not market:
        return None
    raw = market.get("floor_strike")
    if raw is None:
        raw = market.get("cap_strike")  # fallback, shouldn't be needed for up/down markets
    if raw is None:
        return None
    try:
        if isinstance(raw, str):
            raw = raw.replace("$", "").replace(",", "")
        return float(raw)
    except (TypeError, ValueError):
        return None


def get_price_series(client: KalshiClient, series_ticker: str, current_win: "Window", lookback_cycles: int) -> list:
    """
    Return a chronological (oldest -> newest) list of (Window, price_or_None)
    covering `current_win` and the `lookback_cycles` windows before it, using
    each window's own floor_strike as the BTC reference price at that point.
    """
    windows = []
    w = current_win
    for _ in range(lookback_cycles + 1):
        windows.append(w)
        w = previous_window(w)
    windows.reverse()

    markets = find_markets_for_windows(client, series_ticker, windows)
    return [(w, get_strike_price(markets.get(w))) for w in windows]


def decide_price_trend_side(series: list, threshold_pct: float) -> tuple:
    """
    Given a chronological price series [(Window, price_or_None), ...], compute
    the % change from the oldest available price to the newest, and decide:
      - change >= +threshold_pct  -> ('yes', pct_change)   [bet UP]
      - change <= -threshold_pct  -> ('no', pct_change)    [bet DOWN]
      - otherwise                 -> (None, pct_change)    [skip, no clear trend]
      - insufficient data         -> (None, None)          [skip, can't compute]
    """
    prices = [p for _, p in series if p is not None]
    if len(prices) < 2:
        return None, None
    oldest, newest = prices[0], prices[-1]
    if not oldest:
        return None, None
    pct_change = (newest - oldest) / oldest * 100.0
    if pct_change >= threshold_pct:
        return "yes", pct_change
    if pct_change <= -threshold_pct:
        return "no", pct_change
    return None, pct_change


def check_momentum_filter(price_history: list, lookback_sec: float, side: str, now: Optional[float] = None) -> tuple:
    """
    Checks whether `side` (the direction some strategy has already chosen)
    agrees with the SHORT-TERM BTC price trend over the last `lookback_sec`
    seconds, using a rolling buffer of (monotonic_timestamp, price) samples.
    Intended as a safety gate applied on top of any strategy's decision -
    e.g. don't place an UP order if BTC has been trending DOWN over the last
    30 seconds, regardless of why the strategy picked UP.

    Returns (allowed, recent_direction):
      - recent_direction: 'yes' (trending up), 'no' (trending down), or None
        (not enough data yet, or price hasn't moved either way in the window)
      - allowed: True if there's no data yet / no clear trend (doesn't block
        on ambiguity), or if `side` matches recent_direction. False only when
        there IS a clear recent trend and it's the OPPOSITE of `side`.
    """
    now = now if now is not None else price_history[-1][0] if price_history else 0.0
    cutoff = now - lookback_sec
    recent = [(t, p) for t, p in price_history if t >= cutoff]
    if len(recent) < 2:
        return True, None  # not enough samples yet - don't block on insufficient data

    oldest_price = recent[0][1]
    newest_price = recent[-1][1]
    if newest_price > oldest_price:
        direction = "yes"
    elif newest_price < oldest_price:
        direction = "no"
    else:
        return True, None  # flat - no clear direction, nothing to block on

    return side == direction, direction


def compute_recovery_size(
    cumulative_loss_cents: int, price_cents: int, fee_per_contract_cents: int,
    min_profit_cents: int, max_contracts: int,
) -> Optional[int]:
    """
    Given a live ask price and the loss accumulated so far in this streak,
    compute the number of contracts (at that price) needed so that a WIN on
    this bet fully recovers the cumulative loss plus a minimum profit,
    accounting for Kalshi's per-contract fee (charged on the buy, not on
    settlement/redemption).

    Each winning contract nets (100 - price - fee) cents (redemption pays
    100c flat; fee only applies at purchase). We need:
        N * (100 - price - fee) >= cumulative_loss + min_profit
        N >= (cumulative_loss + min_profit) / (100 - price - fee)

    Returns the required contract count (rounded up), or None if:
      - the price is too high for ANY quantity to recover profitably
        (100 - price - fee <= 0), or
      - the required count exceeds max_contracts (price is technically
        profitable but would need an oversized position to recover).
    In either case the caller should keep waiting for a better price rather
    than place this bet.
    """
    net_per_contract = 100 - price_cents - fee_per_contract_cents
    if net_per_contract <= 0:
        return None

    needed = cumulative_loss_cents + min_profit_cents
    if needed <= 0:
        return 1  # nothing to recover; shouldn't normally be called in this state, but handle gracefully

    required = -(-needed // net_per_contract)  # ceiling division, integers only
    if required > max_contracts:
        return None
    return max(1, required)


def decide_spot_lean_side(spot_price: Optional[float], target_price: Optional[float], threshold_pct: float = 0.0) -> tuple:
    """
    Compare the live BTC spot price against the window's own target (its
    floor_strike - the price it needs to close above for UP to win) and bet
    the side BTC is currently on:
      - spot > target (by more than threshold_pct)  -> ('yes', gap_pct)  [UP]
      - spot < target (by more than threshold_pct)  -> ('no', gap_pct)   [DOWN]
      - within threshold_pct of target, or missing data -> (None, gap_pct_or_None)
    threshold_pct=0.0 (default) means ANY nonzero gap decides a side, matching
    "if BTC is higher than target, bet UP; if lower, bet DOWN" literally.
    """
    if spot_price is None or target_price is None or not target_price:
        return None, None
    gap_pct = (spot_price - target_price) / target_price * 100.0
    if gap_pct > threshold_pct:
        return "yes", gap_pct
    if gap_pct < -threshold_pct:
        return "no", gap_pct
    return None, gap_pct


def decide_late_fade_side(spot_price: Optional[float], target_price: Optional[float], threshold_pct: float = 0.0) -> tuple:
    """
    The mirror image of decide_spot_lean_side: instead of betting WITH the
    current lean, this bets on a REVERSAL back toward the window's own
    target (floor_strike) - used by the "late_fade" strategy, which watches
    for this signal specifically in the closing minutes of a window.

      - spot > target (by more than threshold_pct) -> ('no', gap_pct)  [bet DOWN, anticipating reversion]
      - spot < target (by more than threshold_pct) -> ('yes', gap_pct) [bet UP, anticipating reversion]
      - within threshold_pct of target, or missing data -> (None, gap_pct_or_None)

    gap_pct is returned as-is (not flipped) so callers can log/display the
    actual spot-vs-target relationship, not the inverted bet direction.
    """
    lean_side, gap_pct = decide_spot_lean_side(spot_price, target_price, threshold_pct)
    if lean_side is None:
        return None, gap_pct
    return ("no" if lean_side == "yes" else "yes"), gap_pct


def both_sides_too_expensive(up_price_cents: Optional[int], down_price_cents: Optional[int], max_price_cents: int) -> bool:
    """
    True if BOTH the UP and DOWN ask prices are known and each exceeds
    max_price_cents - used by the "late_fade" strategy's session-skip gate:
    if neither side is cheap enough for a bet even at entry_start_min, no
    price drop is being anticipated during the remaining entry window, so
    the whole session is skipped outright instead of watched all the way to
    entry_end_min.

    Returns False (don't skip) if either price is still unknown - a missing
    price isn't evidence that a window should be skipped.
    """
    if up_price_cents is None or down_price_cents is None:
        return False
    return up_price_cents > max_price_cents and down_price_cents > max_price_cents


def compute_smart_hedge_count(
    this_side_count: int, this_side_cost_cents: int, other_side_cost_cents: int,
    price_cents: int, fee_per_contract_cents: int, min_profit_cents: int,
    max_contracts: Optional[int] = None,
) -> tuple:
    """
    Computes the minimum hedge size (on `this_side`) so that IF this side wins,
    its payout - combined with any contracts already held on this same side
    this session - covers everything already spent on the OTHER side (which
    would be a total loss in that outcome) plus a minimum profit. This is the
    same recovery-style formula as compute_recovery_size, generalized to use
    "money already committed to both sides of this session" as the deficit
    to overcome, instead of a persisted cumulative_loss_cents.

    Returns (count_needed, guaranteed_profit_cents):
      - count_needed: contracts to buy at price_cents. If the existing count
        on this side would ALREADY cover everything with the target profit
        even without adding more (e.g. from an earlier same-direction hedge),
        returns (None, None) - no additional hedge is needed for this crossing.
      - guaranteed_profit_cents: the resulting profit IF this side wins, after
        buying count_needed more contracts (this is specific to this outcome -
        unlike the old equalize-based approach, the OTHER outcome's profit
        may differ, since counts aren't being matched).
      Returns (None, None) also if the price is too high for any quantity to
      help (100 - price - fee <= 0), or if count_needed would exceed
      max_contracts (when given).
    """
    total_cost_before_this_hedge = this_side_cost_cents + other_side_cost_cents
    shortfall_cents = total_cost_before_this_hedge - this_side_count * 100
    needed_cents = shortfall_cents + min_profit_cents

    if needed_cents <= 0:
        return None, None  # existing count on this side already clears the target; no hedge needed

    net_per_contract = 100 - price_cents - fee_per_contract_cents
    if net_per_contract <= 0:
        return None, None  # price too high for any quantity to help

    count_needed = -(-needed_cents // net_per_contract)  # ceiling division
    if max_contracts is not None and count_needed > max_contracts:
        return None, None

    guaranteed_profit_cents = (this_side_count + count_needed) * 100 - total_cost_before_this_hedge - count_needed * (price_cents + fee_per_contract_cents)
    return count_needed, guaranteed_profit_cents


def compute_take_profit_profit(
    count: int, this_side_cost_cents: int, opposite_price_cents: int, fee_per_contract_cents: int,
) -> int:
    """
    The mirror image of compute_smart_hedge_count: instead of solving for the
    hedge count needed to cover a loss, this checks a FIXED count (the main
    bet's own count) and returns the guaranteed profit (cents) if that many
    contracts are bought on the OPPOSITE side right now, at opposite_price_cents.

    Since both sides then hold the identical count, the payout is exactly
    count * 100 regardless of which side the window ultimately settles on -
    this is what makes it a real profit LOCK rather than another strategy bet:
    once placed, the outcome no longer matters to this session's P&L.

        guaranteed_profit = count*100 - (this_side_cost_cents + count*(opposite_price_cents + fee))

    The caller should only act on this if the result is >= their configured
    min_profit_cents - a low (or negative) result just means the market
    hasn't moved far enough in our favor yet for locking in to be worthwhile.
    """
    opposite_cost_cents = count * (opposite_price_cents + fee_per_contract_cents)
    return count * 100 - (this_side_cost_cents + opposite_cost_cents)


SIDE_LABEL = {"yes": "UP", "no": "DOWN"}


def label(side: Optional[str]) -> str:
    if side is None:
        return "UNKNOWN"
    return SIDE_LABEL.get(side, side.upper())


from decimal import Decimal, ROUND_HALF_UP, InvalidOperation


def _dollars_to_cents(value) -> Optional[int]:
    """Convert a fixed-point dollar string (e.g. '0.5600') to integer cents."""
    if value is None:
        return None
    try:
        return int((Decimal(str(value)) * 100).to_integral_value(rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return None


def get_quote(market: dict) -> dict:
    """
    Return {yes_bid, yes_ask, no_bid, no_ask} in integer cents.
    Kalshi's current API returns fixed-point '<field>_dollars' strings
    (e.g. yes_ask_dollars='0.5600'); older responses used plain integer
    cents fields (yes_ask=56). This reads whichever is present.
    """
    quote = {}
    for key in ("yes_bid", "yes_ask", "no_bid", "no_ask"):
        cents = _dollars_to_cents(market.get(f"{key}_dollars"))
        if cents is None:
            cents = market.get(key)  # legacy integer-cents fallback
        quote[key] = cents
    return quote


def _best_bid_cents(orderbook: dict, side: str) -> Optional[int]:
    """
    Return the best (highest) resting bid price, in cents, for `side` ('yes' or 'no'),
    from an orderbook_fp response. Levels are [price, count] pairs sorted ascending,
    so the best bid is the last entry with a nonzero count.
    Handles both fixed-point ('yes_dollars'/'no_dollars', string prices) and legacy
    integer-cents ('yes'/'no', int prices) orderbook shapes.
    """
    key_dollars = f"{side}_dollars"
    key_legacy = side
    levels = orderbook.get(key_dollars)
    is_dollars = levels is not None
    if levels is None:
        levels = orderbook.get(key_legacy)
    if not levels:
        return None

    for price_raw, count_raw in reversed(levels):
        try:
            count = Decimal(str(count_raw))
        except (InvalidOperation, ValueError):
            count = None
        if count is not None and count <= 0:
            continue  # skip empty/zero-size levels
        if is_dollars:
            return _dollars_to_cents(price_raw)
        try:
            return int(price_raw)
        except (TypeError, ValueError):
            return None
    return None


def count_alternations(results: list) -> int:
    """
    Given a chronological list of window results ('yes'/'no'), count how
    many times consecutive results differed (flipped direction).
    e.g. ['yes','no','yes','no'] -> 3 (every step flipped = pure chop).
         ['yes','yes','yes','no'] -> 1 (mostly trending, one flip).
    """
    return sum(1 for a, b in zip(results, results[1:]) if a != b)


def price_from_orderbook(orderbook: dict, side: str) -> Optional[int]:
    """
    Return the price (cents) to BUY `side`, derived from the live orderbook.
    Kalshi's orderbook only lists bids; the ask for one side is the complement
    of the opposite side's best bid (yes_ask = 100 - best no_bid, and vice
    versa). Returns None if the opposite side's book is empty (no price
    can be determined yet - thin/fresh market).
    """
    opposite = "no" if side == "yes" else "yes"
    opposite_best_bid = _best_bid_cents(orderbook, opposite)
    if opposite_best_bid is None:
        return None
    return 100 - opposite_best_bid


def current_price_cents(market: dict, side: str) -> Optional[int]:
    """
    Return the current ask price (cents) to BUY the given side ('yes' or 'no').
    Falls back to deriving from the opposite side's bid if the direct ask
    field is missing (yes_ask ~= 100 - no_bid, and vice versa - Kalshi's
    YES+NO=100c duality).
    """
    quote = get_quote(market)
    if side == "yes":
        price = quote.get("yes_ask")
        if price is None and quote.get("no_bid") is not None:
            price = 100 - quote["no_bid"]
    elif side == "no":
        price = quote.get("no_ask")
        if price is None and quote.get("yes_bid") is not None:
            price = 100 - quote["yes_bid"]
    else:
        raise ValueError(f"Unknown side: {side}")
    return price


def winning_side(market: dict) -> Optional[str]:
    """
    Return 'yes' or 'no' for the settled winning side, or None if not settled yet.
    Kalshi settled markets expose a 'result' field: 'yes' or 'no'.
    """
    result = market.get("result")
    if result in ("yes", "no"):
        return result
    # Fallback: some responses use status + last traded/settlement price fields.
    if market.get("status") == "finalized":
        return market.get("result")
    return None


def decide_side(previous_result: str, mode: str) -> str:
    """
    previous_result: 'yes' (UP won) or 'no' (DOWN won)
    mode: 'momentum' -> same side, 'reversal' -> opposite side
    """
    if mode == "momentum":
        return previous_result
    if mode == "reversal":
        return "no" if previous_result == "yes" else "yes"
    raise ValueError(f"Unknown strategy mode: {mode}")
