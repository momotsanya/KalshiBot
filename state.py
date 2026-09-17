# V1.6
"""
Persists martingale stake + the pending bet so the bot can resume correctly
after a restart (crucial: martingale sizing depends on the outcome of the
last bet, so this state must survive process restarts).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, asdict, field
from typing import Optional

log = logging.getLogger("state")


@dataclass
class PendingBet:
    window_close_iso: str   # ISO timestamp of the window this bet was placed on
    ticker: str
    side: str               # "yes" or "no"
    stake: float            # contracts or dollars, per sizing.mode
    price_cents: int        # actual price paid, for accurate PnL
    order_id: Optional[str] = None


@dataclass
class BotState:
    current_stake: float = 1.0
    consecutive_losses: int = 0
    pending_bets: list = field(default_factory=list)  # list of serialized PendingBet - normally 0-1, up to 2 with spot_lean hedging
    total_wins: int = 0
    total_losses: int = 0
    total_pnl_cents: int = 0
    recent_results: list = field(default_factory=list)  # chronological 'yes'/'no', for chop detection
    skipped_chop: int = 0
    last_bet_won: Optional[bool] = None  # for adaptive mode: outcome of our most recent settled bet
    cumulative_loss_cents: int = 0  # for sizing.mode="recovery": total unrecovered loss (incl. fees) in the current losing streak
    recovery_attempts: int = 0  # this naturally counts "losses in the row" at the session level a losing session increments it, a fully-recovered session or a cap-triggered reset zeroes it
    max_drawdown_cents: int = 0  # peak (cumulative_loss_cents + cost of the bet just placed) ever reached
    dalembert_pnl_cents: int = 0  # for sizing.mode="dalembert" or "dalembert_reverse": running net PnL since the last profit-lock/loss-floor reset (or fresh start) - shared by both, since sizing.profit_lock_cents/loss_floor_cents are now shared config knobs (see record_dalembert_result / record_dalembert_reverse_result)
    consecutive_wins: int = 0  # for sizing.mode="anti_martingale": current win streak length, reset on any loss (mirrors consecutive_losses for classic martingale)
    # Mode-agnostic running total of cost paid on CONSECUTIVE LOSING sessions
    # (a "session" = one settled window's bet, or a main+hedge combo scored
    # together). Used ONLY to compute max_drawdown_cents correctly for
    # EVERY sizing.mode - not just "recovery", whose own cumulative_loss_cents
    # field only tracks that one mode's own bet-sizing debt and stays at 0
    # for every other mode (contracts/dollars/dalembert/dalembert_reverse/
    # anti_martingale), which is what made max_drawdown_cents collapse to
    # "just the last bet's own cost" for those modes (see bot.py V1.8 fix).
    # Resets to 0 the instant a session's net result is a win.
    current_loss_streak_cost_cents: int = 0

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @staticmethod
    def from_json(s: str) -> "BotState":
        d = json.loads(s)
        if "pending_bet" in d and "pending_bets" not in d:
            # Migrate from the old single-bet state format
            legacy = d.pop("pending_bet")
            d["pending_bets"] = [legacy] if legacy else []
        if "dalembert_reverse_pnl_cents" in d and "dalembert_pnl_cents" not in d:
            # Migrate from before profit-lock/loss-floor were shared between
            # dalembert and dalembert_reverse (the field used to be reverse-only)
            d["dalembert_pnl_cents"] = d.pop("dalembert_reverse_pnl_cents")
        return BotState(**d)


class StateStore:
    def __init__(self, path: str, base_stake: float):
        self.path = path
        self.base_stake = base_stake
        self.state = self._load()

    def _load(self) -> BotState:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r") as f:
                    return BotState.from_json(f.read())
            except Exception as e:  # noqa: BLE001
                log.warning("Failed to load state file (%s), starting fresh: %s", self.path, e)
        return BotState(current_stake=self.base_stake)

    def save(self):
        with open(self.path, "w") as f:
            f.write(self.state.to_json())

    def add_pending_bet(self, bet: PendingBet):
        """Adds a bet to track until settlement. Normally there's one per window; spot_lean
        hedging can add a second (opposite-side) bet for the same window."""
        self.state.pending_bets.append(asdict(bet))
        self.save()

    def clear_pending_bets(self):
        self.state.pending_bets = []
        self.save()

    def record_window_result(self, result: str, max_history: int = 20):
        """Append a settled window's result to the rolling history (for chop detection)."""
        self.state.recent_results.append(result)
        if len(self.state.recent_results) > max_history:
            self.state.recent_results = self.state.recent_results[-max_history:]
        self.save()

    def update_max_drawdown(self, candidate_cents: int):
        """
        candidate_cents = cumulative_loss_cents (prior, unresolved losses) + cost of
        the bet just placed - i.e. the total cash tied up/at risk at that moment,
        before this bet's outcome is known. Tracks the peak ever reached.
        """
        if candidate_cents > self.state.max_drawdown_cents:
            self.state.max_drawdown_cents = candidate_cents
        self.save()

    def update_loss_streak_cost(self, session_cost_cents: int, session_won: bool):
        """
        Call once per settled session, right alongside update_max_drawdown(),
        regardless of sizing.mode. Resets the running streak-cost total to 0
        on a win; otherwise adds this session's own cost on top of whatever
        the current consecutive-loss streak already owes, so the NEXT
        session's drawdown check correctly includes every prior consecutive
        loss's cost - not just its own. This is what update_max_drawdown()'s
        candidate_cents should be built from for every sizing.mode; see
        bot.py's score_pending_bets() for the call site.
        """
        if session_won:
            self.state.current_loss_streak_cost_cents = 0
        else:
            self.state.current_loss_streak_cost_cents += session_cost_cents
        self.save()

    def record_recovery_result(self, net_pnl_cents: int, max_cumulative_loss_cents: int):
        """
        Update cumulative_loss_cents for sizing.mode="recovery", based on the
        NET pnl of a settled session (a single bet, or - with spot_lean hedging -
        the combined outcome of a main + hedge bet(s) on the same window).

        cumulative_loss_cents is a running deficit owed across ALL sessions,
        not just this one - so it's reduced by net_pnl_cents, not simply reset
        to zero whenever this session happened to be profitable. A session
        that made $0.99 but was chasing a $1.93 deficit still leaves $0.94
        owed; only a session whose net_pnl fully covers what was owed clears
        it to zero (and anything beyond that is a real bonus profit, not
        carried forward as "negative debt").

        For a single, non-hedged bet, compute_recovery_size always sizes the
        bet so that a WIN's net_pnl covers the full existing deficit plus
        min_profit - so this is exactly equivalent to the old "reset to zero
        on any win" behavior in that case. The difference only shows up when
        a hedge changes the session's net result away from what the recovery
        math originally targeted (which is exactly the bug this fixes).

        recovery_attempts counts consecutive LOSING sessions in the current
        streak (i.e. sessions that didn't fully clear cumulative_loss_cents) -
        reset to 0 on a fresh start, whether that's from fully recovering or
        from hitting the safety cap below.
        """
        self.state.cumulative_loss_cents = max(0, self.state.cumulative_loss_cents - net_pnl_cents)
        fully_recovered = self.state.cumulative_loss_cents == 0
        self.state.last_bet_won = fully_recovered
        if fully_recovered:
            self.state.recovery_attempts = 0
        else:
            self.state.recovery_attempts += 1
            if self.state.cumulative_loss_cents > max_cumulative_loss_cents:
                log.warning(
                    "Recovery mode: cumulative loss $%.2f exceeded safety cap $%.2f - giving up on recovering "
                    "this streak and resetting to a fresh start instead of chasing it further.",
                    self.state.cumulative_loss_cents / 100, max_cumulative_loss_cents / 100,
                )
                self.state.cumulative_loss_cents = 0
                self.state.recovery_attempts = 0
                self.state.last_bet_won = True
        self.save()

    def record_dalembert_result(
        self, won: bool, net_pnl_cents: int, unit: float, max_stake: float,
        profit_lock_cents: int = 0, loss_floor_cents: int = 0,
    ):
        """
        D'Alembert sizing: a gentler progression than martingale's
        multiplicative doubling - increases the stake by one `unit` after a
        loss, decreases it by one `unit` after a win. Floors at one `unit`
        (never drops below a fresh-start stake, so it can't go to zero or
        negative) and is capped at max_stake as a safety ceiling, same as
        record_result()'s martingale cap.

        Shares the same optional profit-lock / loss-floor safety net as
        record_dalembert_reverse_result(), via the same dalembert_pnl_cents
        running counter (sizing.profit_lock_cents / sizing.loss_floor_cents
        apply to BOTH "dalembert" and "dalembert_reverse" - see config.yaml):
          - profit_lock_cents: once real net PnL since the last reset reaches
            this much profit, lock it in and reset to a fresh start.
          - loss_floor_cents: once it drops to this much loss (a positive
            magnitude; compared against the counter going <= -loss_floor_cents),
            cut the losing run short and reset.
        Either set to 0 (or negative) disables that check - the stake then
        just steps up/down indefinitely between `unit` and max_stake.
        """
        self.state.last_bet_won = won
        self.state.dalembert_pnl_cents += net_pnl_cents

        if won:
            self.state.current_stake = max(unit, self.state.current_stake - unit)
        else:
            self.state.current_stake = min(self.state.current_stake + unit, max_stake)

        if profit_lock_cents > 0 and self.state.dalembert_pnl_cents >= profit_lock_cents:
            log.info(
                "D'Alembert: profit-lock threshold reached ($%.2f >= $%.2f) - "
                "locking in the gain and resetting to a fresh start.",
                self.state.dalembert_pnl_cents / 100, profit_lock_cents / 100,
            )
            self.state.current_stake = self.base_stake
            self.state.dalembert_pnl_cents = 0
        elif loss_floor_cents > 0 and self.state.dalembert_pnl_cents <= -loss_floor_cents:
            log.warning(
                "D'Alembert: loss-floor threshold reached (-$%.2f <= -$%.2f) - "
                "cutting the losing run short and resetting to a fresh start.",
                abs(self.state.dalembert_pnl_cents) / 100, loss_floor_cents / 100,
            )
            self.state.current_stake = self.base_stake
            self.state.dalembert_pnl_cents = 0

        self.save()

    def record_dalembert_reverse_result(
        self, won: bool, net_pnl_cents: int, unit: float, max_stake: float,
        profit_lock_cents: int, loss_floor_cents: int,
    ):
        """
        D'Alembert Reverse: the mirror image of record_dalembert_result() -
        stake increases by one `unit` after a WIN and decreases by one
        `unit` after a LOSS (floored at `unit`, capped at max_stake),
        instead of the classic dalembert's "up on loss, down on win". Shares
        the same `unit` config value (sizing.dalembert_unit) as classic
        dalembert - only the step direction differs.

        On top of the stepping, `dalembert_pnl_cents` accumulates the REAL
        net PnL (not just win/loss) of every settled bet/session since the
        last reset - this is the same running counter classic dalembert's
        own record_dalembert_result() shares, since sizing.profit_lock_cents
        and sizing.loss_floor_cents are now shared config knobs used by
        BOTH "dalembert" and "dalembert_reverse" (see config.yaml), not a
        dalembert_reverse-only setting:
          - profit_lock_cents: once the running counter reaches this much
            profit, lock it in and reset to a fresh start (base_stake, and
            the counter zeroed) instead of letting the stake climb
            indefinitely on a hot streak.
          - loss_floor_cents: once the running counter drops to this much
            loss (given here as a positive magnitude; compared against the
            counter going <= -loss_floor_cents), cut the losing run short
            and reset instead of grinding the stake down by `unit` forever.
        Either threshold set to 0 (or negative) disables that check - the
        stake then just steps up/down indefinitely between `unit` and
        max_stake, same as classic dalembert with no circuit breaker.
        """
        self.state.last_bet_won = won
        self.state.dalembert_pnl_cents += net_pnl_cents

        if won:
            self.state.current_stake = min(self.state.current_stake + unit, max_stake)
        else:
            self.state.current_stake = max(unit, self.state.current_stake - unit)

        if profit_lock_cents > 0 and self.state.dalembert_pnl_cents >= profit_lock_cents:
            log.info(
                "D'Alembert Reverse: profit-lock threshold reached ($%.2f >= $%.2f) - "
                "locking in the gain and resetting to a fresh start.",
                self.state.dalembert_pnl_cents / 100, profit_lock_cents / 100,
            )
            self.state.current_stake = self.base_stake
            self.state.dalembert_pnl_cents = 0
        elif loss_floor_cents > 0 and self.state.dalembert_pnl_cents <= -loss_floor_cents:
            log.warning(
                "D'Alembert Reverse: loss-floor threshold reached (-$%.2f <= -$%.2f) - "
                "cutting the losing run short and resetting to a fresh start.",
                abs(self.state.dalembert_pnl_cents) / 100, loss_floor_cents / 100,
            )
            self.state.current_stake = self.base_stake
            self.state.dalembert_pnl_cents = 0

        self.save()

    def record_anti_martingale_result(
        self, won: bool, variant: str, unit: float, multiplier: float, max_stake: float, max_steps: int,
    ):
        """
        Anti-martingale ("reverse martingale") sizing: the mirror image of
        classic martingale - grows the stake while WINNING and snaps
        straight back to a fresh start (self.base_stake) after ANY loss,
        instead of growing after losses and resetting after wins.

        variant="plus": stake increases by one fixed `unit` after each win.
        variant="multiplier" (or anything else): stake is multiplied by
          `multiplier` after each win.
        Either way, the growing stake is capped at max_stake.

        max_steps is the win-streak mirror of record_result()'s own
        max_steps circuit breaker: consecutive_wins counts the CURRENT win
        streak (reset to 0 on any loss, same as consecutive_losses resets
        to 0 on any win over there). Once a streak exceeds max_steps, this
        win resets straight back to a fresh start instead of growing the
        stake further - a safety cap so a hot streak can't ride the stake
        up indefinitely even before max_stake itself would kick in.
        """
        self.state.last_bet_won = won
        if won:
            self.state.consecutive_wins += 1
            if self.state.consecutive_wins > max_steps:
                log.warning(
                    "Max anti-martingale steps (%s) exceeded - resetting to base stake instead of growing further.",
                    max_steps,
                )
                self.state.current_stake = self.base_stake
                self.state.consecutive_wins = 0
            elif variant == "plus":
                self.state.current_stake = min(self.state.current_stake + unit, max_stake)
            else:
                self.state.current_stake = min(self.state.current_stake * multiplier, max_stake)
        else:
            self.state.current_stake = self.base_stake
            self.state.consecutive_wins = 0
        self.save()

    def record_result(
        self, won: bool, multiplier: float, max_steps: int, max_stake: float,
        variant: str = "multiplier", unit: float = 1,
    ):
        """
        Update classic martingale stake after a bet (or, with net-session sizing
        enabled, a whole session's net outcome) resolves.

        variant="multiplier" (default, unchanged behavior): stake is
          multiplied by `multiplier` after each loss (classic doubling).
        variant="plus": stake instead increases by one fixed `unit` after
          each loss - a gentler, linear growth instead of exponential, the
          same "+1 per step" idea sizing.mode="anti_martingale" already
          offers for win streaks (see record_anti_martingale_result), just
          applied to martingale's loss side instead.
        Either way: resets straight to base_stake on any win, and
        max_martingale_steps/max_stake still apply as the same circuit
        breakers regardless of which variant grew the stake.
        """
        self.state.last_bet_won = won
        if won:
            self.state.consecutive_losses = 0
            self.state.current_stake = self.base_stake
        else:
            self.state.consecutive_losses += 1
            if self.state.consecutive_losses > max_steps:
                log.warning(
                    "Max martingale steps (%s) exceeded - resetting to base stake instead of growing further.",
                    max_steps,
                )
                self.state.current_stake = self.base_stake
                self.state.consecutive_losses = 0
            elif variant == "plus":
                self.state.current_stake = min(self.state.current_stake + unit, max_stake)
            else:
                self.state.current_stake = min(self.state.current_stake * multiplier, max_stake)
        self.save()
