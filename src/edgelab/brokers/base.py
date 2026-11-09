"""Broker interface. Every venue (cTrader, IBKR, a crypto exchange, ...) implements this.

The trading engine only talks to this interface, so the exact same decision
code runs in backtests (PaperBroker) and against a real demo/live account.

Adapter checklist (see README):
  * call mode.assert_account_matches_mode() before the first order
  * attach the stop-loss to the order itself (server-side), never "later"
  * make submit_market idempotent / protect against duplicate orders on reconnect
  * report stop-outs through pop_events()
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd

from ..mode import ExecutionMode


@dataclass
class Position:
    direction: int            # +1 long, -1 short
    qty: float
    entry_price: float        # actual fill price (includes costs)
    stop: float
    entry_time: pd.Timestamp
    risk_amount: float
    entry_fee: float = 0.0


@dataclass
class CloseEvent:
    direction: int
    reason: str               # "stop", "stop_gap", "signal_exit", "kill_switch", "end_of_data"


class Broker(ABC):
    mode: ExecutionMode
    # Simulated brokers (PaperBroker) model fills internally and are advanced bar-by-bar by the
    # caller via begin_bar/end_bar. Real venue adapters (cTrader, ...) get fills from the venue,
    # so the runner must not try to drive their bar lifecycle. The DemoRunner branches on this.
    is_simulated: bool = False

    @abstractmethod
    def position(self) -> Position | None: ...

    @abstractmethod
    def equity(self) -> float: ...

    @abstractmethod
    def reference_price(self) -> float:
        """Price used for sizing the next market order."""

    @abstractmethod
    def submit_market(self, direction: int, qty: float, stop_distance: float, risk_amount: float) -> Position | None:
        """Place a market order. Returns the Position once filled, or None if the order wasn't
        filled (rejected, or market closed) — in which case the broker stays flat."""

    @abstractmethod
    def close_position(self, reason: str) -> None: ...

    @abstractmethod
    def pop_events(self) -> list[CloseEvent]:
        """Return and clear close events (stop-outs etc.) since the last call."""

    def pop_realized(self) -> list[dict]:
        """Return and clear realized-trade records since the last call, each
        {pnl, reason, dir}. Used to surface per-trade profit/loss in notifications.
        Default: none (brokers that track P&L override this)."""
        return []

    def pop_rejections(self) -> list[dict]:
        """Return and clear rejected-order records since the last call, each
        {symbol, side, reason}. Used to alert when an order didn't place.
        Default: none (brokers that can be rejected override this)."""
        return []
