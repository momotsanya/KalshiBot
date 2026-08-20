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
    recovery_attempts: int = 0  # consecutive windows where no qualifying recovery price/size was found
    max_drawdown_cents: int = 0  # peak (cumulative_loss_cents + cost of the bet just placed) ever reached

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @staticmethod
    def from_json(s: str) -> "BotState":
        d = json.loads(s)
        if "pending_bet" in d and "pending_bets" not in d:
            # Migrate from the old single-bet state format
            legacy = d.pop("pending_bet")
            d["pending_bets"] = [legacy] if legacy else []
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
        """
        self.state.cumulative_loss_cents = max(0, self.state.cumulative_loss_cents - net_pnl_cents)
        fully_recovered = self.state.cumulative_loss_cents == 0
        self.state.last_bet_won = fully_recovered
        if fully_recovered:
            self.state.recovery_attempts = 0
        elif self.state.cumulative_loss_cents > max_cumulative_loss_cents:
            log.warning(
                "Recovery mode: cumulative loss $%.2f exceeded safety cap $%.2f - giving up on recovering "
                "this streak and resetting to a fresh start instead of chasing it further.",
                self.state.cumulative_loss_cents / 100, max_cumulative_loss_cents / 100,
            )
            self.state.cumulative_loss_cents = 0
            self.state.recovery_attempts = 0
            self.state.last_bet_won = True
        self.save()

    def record_result(self, won: bool, multiplier: float, max_steps: int, max_stake: float):
        """Update classic martingale stake after a bet (or, with net-session sizing
        enabled, a whole session's net outcome) resolves."""
        self.state.last_bet_won = won
        if won:
            self.state.consecutive_losses = 0
            self.state.current_stake = self.base_stake
        else:
            self.state.consecutive_losses += 1
            if self.state.consecutive_losses > max_steps:
                log.warning(
                    "Max martingale steps (%s) exceeded - resetting to base stake instead of doubling further.",
                    max_steps,
                )
                self.state.current_stake = self.base_stake
                self.state.consecutive_losses = 0
            else:
                self.state.current_stake = min(self.state.current_stake * multiplier, max_stake)
        self.save()
