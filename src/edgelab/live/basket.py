"""BasketRunner: trade the same strategy across several markets on ONE account.

Each market is an independent DemoRunner "leg" (its own broker, engine, risk, one position).
A single clock drives all legs one bar at a time, and an account-level kill switch flattens
everything if total equity draws down too far — the backstop that per-leg limits can't see.

Works with simulated legs (PaperBroker = shadow/offline) and live legs (CTraderBroker), exactly
like the single-symbol runner, so you shadow the basket before placing real orders.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable

import pandas as pd

from ..risk import RiskConfig, RiskEngine
from ..models import EURUSD
from .runner import DemoRunner
from .sources import ReplaySource

log = logging.getLogger("edgelab.basket")


class BarClock(ABC):
    @abstractmethod
    def wait_next(self) -> pd.Timestamp | None:
        """Block until the next bar is ready across all legs; return its ts, or None when done."""


class ReplayClock(BarClock):
    """Drives several ReplaySources in lockstep (tests / offline basket dry-runs)."""

    def __init__(self, sources: list[ReplaySource]):
        self.sources = sources

    def wait_next(self) -> pd.Timestamp | None:
        ts = None
        for s in self.sources:
            t = s.wait_next_closed()
            if t is None:
                return None
            ts = ts or t
        return ts


class CTraderClock(BarClock):
    """Waits once per bar on the shared cTrader client; live legs then serve their latest bars."""

    def __init__(self, client, timeframe: str, ref_symbol: str):
        self.client = client
        self.timeframe = timeframe
        self.ref_symbol = ref_symbol
        self._bars = 0
        self.max_bars = None

    def wait_next(self) -> pd.Timestamp | None:
        if self.max_bars is not None and self._bars >= self.max_bars:
            return None
        self._bars += 1
        return self.client.wait_next_closed_bar(self.ref_symbol, self.timeframe)


@dataclass
class BasketConfig:
    account_max_drawdown_pct: float = 20.0   # account-level kill switch (flatten ALL, stop)
    account_daily_loss_pct: float = 5.0


class BasketRunner:
    def __init__(
        self,
        clock: BarClock,
        legs: list[DemoRunner],
        equity_fn: Callable[[], float],
        cfg: BasketConfig | None = None,
        on_event: Callable[[str, dict], None] | None = None,
    ):
        if not legs:
            raise ValueError("BasketRunner needs at least one leg")
        self.clock = clock
        self.legs = legs
        self.equity_fn = equity_fn
        self.cfg = cfg or BasketConfig()
        self.on_event = on_event or (lambda kind, data: None)
        for leg in legs:          # funnel every leg's events through the basket's single handler
            leg.on_event = self.on_event
        # account-level risk monitor (sizing fields unused; only drawdown/daily tracking)
        self.account_risk = RiskEngine(
            RiskConfig(risk_per_trade_pct=0.5,
                       daily_loss_limit_pct=self.cfg.account_daily_loss_pct,
                       max_drawdown_halt_pct=self.cfg.account_max_drawdown_pct,
                       max_leverage=1e9),
            EURUSD)
        self.stopped_reason: str | None = None

    def run(self) -> None:
        while True:
            ts = self.clock.wait_next()
            if ts is None:
                break
            for leg in self.legs:
                leg.process(ts)

            eq = self.equity_fn()
            self.account_risk.update(ts, eq)
            self.on_event("account", {"ts": str(ts), "equity": round(eq, 2),
                                      "kill_switch": self.account_risk.kill_switch})
            if self.account_risk.kill_switch:
                self.stopped_reason = self.account_risk.kill_reason
                self.on_event("account_kill", {"ts": str(ts), "reason": self.account_risk.kill_reason,
                                               "equity": round(eq, 2)})
                for leg in self.legs:
                    if leg.broker.position() is not None:
                        leg.broker.close_position("account_kill")
                break

        for leg in self.legs:
            leg.final_flatten()
