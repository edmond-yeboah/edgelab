"""DemoRunner: drives the SAME TradingEngine used in backtests, bar by bar, in real time.

Loop, once per closed candle:
  1. pull recent history (closed bars only)
  2. recompute strategy signals on that history (causal)
  3. feed the latest completed bar's signal to the engine
  4. let the broker settle the bar; update risk; log

Because it reuses TradingEngine + RiskEngine, the live decision path is identical to
the backtester. tests/test_live_runner.py proves a ReplaySource run reproduces
run_backtest trade-for-trade.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

import pandas as pd

from ..brokers.base import Broker
from ..engine import TradingEngine
from ..mode import assert_account_matches_mode
from ..models import Instrument
from ..risk import RiskEngine, RiskConfig
from ..strategies.base import Strategy
from .sources import MarketDataSource

log = logging.getLogger("edgelab.runner")

_PERIOD_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}


def _period_delta(timeframe: str) -> pd.Timedelta:
    return pd.Timedelta(minutes=_PERIOD_MINUTES.get(timeframe, 60))


@dataclass
class RunnerConfig:
    lookback: int = 1500          # bars of history kept for signal computation
    reentry_cooldown_bars: int = 1
    max_bars: int | None = None   # stop after N bars (None = until source exhausted)


class DemoRunner:
    def __init__(
        self,
        source: MarketDataSource,
        broker: Broker,
        strategy: Strategy,
        risk_cfg: RiskConfig,
        instrument: Instrument,
        cfg: RunnerConfig | None = None,
        account_is_live: bool | None = None,
        on_event: Callable[[str, dict], None] | None = None,
        name: str = "",
    ):
        self.source = source
        self.broker = broker
        self.strategy = strategy
        self.instrument = instrument
        self.name = name
        self.cfg = cfg or RunnerConfig()
        self.risk = RiskEngine(risk_cfg, instrument)
        self.engine = TradingEngine(broker, self.risk, reentry_cooldown_bars=self.cfg.reentry_cooldown_bars)
        self.on_event = on_event or (lambda kind, data: None)
        # Fail-closed: refuse to run if the broker's account mode doesn't match its declared mode.
        if not broker.is_simulated:
            assert_account_matches_mode(broker.mode, account_is_live)
        if self.cfg.lookback <= self.strategy.warmup_bars:
            raise ValueError(f"lookback ({self.cfg.lookback}) must exceed strategy warmup "
                             f"({self.strategy.warmup_bars}) or signals are never valid")

    def run(self) -> None:
        bars = 0
        while True:
            ts = self.source.wait_next_closed()
            if ts is None:
                break
            if not self.process(ts):
                continue
            bars += 1
            if self.cfg.max_bars is not None and bars >= self.cfg.max_bars:
                break
        self.final_flatten()

    def process(self, ts) -> bool:
        """Process one ALREADY-CLOSED bar (no waiting). Used standalone and by BasketRunner.

        The caller is responsible for advancing to this bar first (source.wait_next_closed in
        run(), or the basket clock). Returns True if a bar was processed.
        """
        hist = self.source.history(self.cfg.lookback)
        if len(hist) == 0:
            return False
        self._on_closed_bar(hist)
        self._emit_heartbeat(ts)
        return True

    def final_flatten(self) -> None:
        # flatten any simulated open position so metrics are complete (real venues keep the position)
        if self.broker.is_simulated and self.broker.position() is not None:
            last_close = float(self.source.history(1).iloc[-1]["close"])
            self.broker.force_close_at(last_close, "end_of_data")

    def _on_closed_bar(self, hist: pd.DataFrame) -> None:
        sig = self.strategy.signals(hist)
        last = hist.index[-1]
        direction = int(sig["direction"].iloc[-1])
        stop_distance = float(sig["stop_distance"].iloc[-1])

        if self.broker.is_simulated:
            # Advance the simulated broker through this bar so its fills line up with the signal
            # that was known at the PREVIOUS close (no look-ahead): we act on the prior signal
            # at this bar's open, exactly like the backtester.
            row = hist.iloc[-1]
            prev = self._prev_signal
            self.broker.begin_bar(last, float(row["open"]), float(row["high"]),
                                  float(row["low"]), float(row["close"]))
            if prev is not None:
                self.engine.on_signal(last, prev[0], prev[1])
            self.broker.end_bar()
            self.engine.after_bar(last)
            self._prev_signal = (direction, stop_distance)
        else:
            # Real venue: the broker already reflects fills. Act on THIS just-closed bar's signal
            # at the next tick; the venue fills it. Then mark risk from live equity.
            self.engine.on_signal(last, direction, stop_distance)
            self.engine.after_bar(last)

        if self.risk.kill_switch:
            self.on_event("kill_switch", {"reason": self.risk.kill_reason, "ts": str(last)})

    _prev_signal = None

    def _emit_heartbeat(self, ts) -> None:
        pos = self.broker.position()
        data = {
            "ts": str(ts), "equity": round(self.broker.equity(), 2),
            "position": None if pos is None else {"dir": pos.direction, "qty": pos.qty},
            "kill_switch": self.risk.kill_switch, "daily_halt": self.risk.daily_halt,
        }
        if self.name:
            data = {"symbol": self.name, **data}
        self.on_event("bar", data)
