"""The single decision path shared by backtests and live/demo trading.

Given a (direction, stop_distance) signal computed at the previous bar's close,
decide what to do at the next open. Backtester and demo runner both call
`on_signal`, so there is no second implementation to drift out of sync.
"""

from __future__ import annotations

import math

import pandas as pd

from .brokers.base import Broker
from .risk import RiskEngine


class TradingEngine:
    def __init__(self, broker: Broker, risk: RiskEngine, reentry_cooldown_bars: int = 1):
        self.broker = broker
        self.risk = risk
        # After a stop-out, wait this many bars before re-entering the SAME direction.
        # This avoids thrashing (re-buying on the same bar we were just stopped on) WITHOUT
        # stranding a trend follower: once the cooldown elapses it may re-join an ongoing
        # trend even if the signal never flipped. cooldown=0 would allow same-bar re-entry.
        self.reentry_cooldown_bars = max(0, int(reentry_cooldown_bars))
        self._stopped_dir = 0
        self._cooldown = 0

    def on_signal(self, ts: pd.Timestamp, direction: int, stop_distance: float) -> None:
        self._absorb_events()
        pos = self.broker.position()

        if self.risk.kill_switch:
            if pos is not None:
                self.broker.close_position("kill_switch")
                self._absorb_events()
            return

        if pos is not None and direction != pos.direction:
            self.broker.close_position("signal_exit")
            self._absorb_events()
            pos = None

        # A flip to flat/opposite clears the cooldown immediately (it's a genuinely new signal).
        if self._stopped_dir and direction != self._stopped_dir:
            self._stopped_dir = 0
            self._cooldown = 0

        suppressed = self._stopped_dir != 0 and self._cooldown > 0
        if pos is None and direction != 0 and not suppressed:
            self._stopped_dir = 0  # entering now; clear any stale cooldown state
            equity = self.broker.equity()
            decision = self.risk.size(equity, self.broker.reference_price(), stop_distance, open_positions=0)
            if decision.approved:
                self.broker.submit_market(direction, decision.qty, stop_distance, self.risk.risk_amount(equity))

    def after_bar(self, ts: pd.Timestamp) -> None:
        """Call after each bar closes (or each poll live)."""
        self._absorb_events()
        if self._cooldown > 0:
            self._cooldown -= 1
        eq = self.broker.equity()
        if not math.isnan(eq):
            self.risk.update(ts, eq)

    def _absorb_events(self) -> None:
        for ev in self.broker.pop_events():
            if ev.reason.startswith("stop"):
                self._stopped_dir = ev.direction
                self._cooldown = self.reentry_cooldown_bars
