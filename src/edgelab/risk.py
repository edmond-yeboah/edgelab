"""Deterministic risk engine. Fails closed: when in doubt, reject.

The AI/strategy layers can only *propose* trades. Position size, exposure caps,
daily loss limit and the drawdown kill switch live here, in plain code.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, fields

import pandas as pd

from .models import Instrument


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade_pct: float = 0.5
    daily_loss_limit_pct: float = 2.0
    max_drawdown_halt_pct: float = 15.0
    max_leverage: float = 5.0
    max_open_positions: int = 1

    @classmethod
    def from_dict(cls, d: dict) -> "RiskConfig":
        allowed = {f.name for f in fields(cls)}
        unknown = set(d) - allowed
        if unknown:
            raise ValueError(f"Unknown risk config keys: {sorted(unknown)}")
        return cls(**d)


@dataclass
class Decision:
    approved: bool
    qty: float = 0.0
    reason: str = "ok"


class RiskEngine:
    def __init__(self, cfg: RiskConfig, instrument: Instrument):
        self.cfg = cfg
        self.instrument = instrument
        self.kill_switch = False
        self.kill_reason: str | None = None
        self.daily_halt = False
        self.events: list[dict] = []
        self.rejections: Counter[str] = Counter()
        self._peak: float | None = None
        self._day = None
        self._day_start_equity: float | None = None

    # ---- state tracking -------------------------------------------------
    def update(self, ts: pd.Timestamp, equity: float) -> None:
        """Call once per bar / poll with current equity."""
        if not math.isfinite(equity):
            self.trip(ts, "non-finite equity")
            return
        if self._peak is None or equity > self._peak:
            self._peak = equity
        day = ts.date()
        if day != self._day:
            self._day = day
            self._day_start_equity = equity
            self.daily_halt = False
        dd = (self._peak - equity) / self._peak * 100 if self._peak > 0 else 100.0
        if dd >= self.cfg.max_drawdown_halt_pct and not self.kill_switch:
            self.trip(ts, f"max drawdown {dd:.1f}% >= {self.cfg.max_drawdown_halt_pct}%")
        if self._day_start_equity and self._day_start_equity > 0:
            day_loss = (self._day_start_equity - equity) / self._day_start_equity * 100
            if day_loss >= self.cfg.daily_loss_limit_pct and not self.daily_halt:
                self.daily_halt = True
                self.events.append({"ts": ts, "event": "daily_halt", "detail": f"day loss {day_loss:.2f}%"})

    def trip(self, ts, reason: str) -> None:
        self.kill_switch = True
        self.kill_reason = reason
        self.events.append({"ts": ts, "event": "kill_switch", "detail": reason})

    def reset_kill_switch(self) -> None:
        """Manual, deliberate re-arm. Never called automatically."""
        self.kill_switch = False
        self.kill_reason = None
        self._peak = None

    # ---- sizing ---------------------------------------------------------
    def risk_amount(self, equity: float) -> float:
        return equity * self.cfg.risk_per_trade_pct / 100.0

    def _reject(self, reason: str) -> Decision:
        self.rejections[reason] += 1
        return Decision(False, 0.0, reason)

    def size(self, equity: float, price: float, stop_distance: float, open_positions: int) -> Decision:
        if self.kill_switch:
            return self._reject("kill_switch")
        if self.daily_halt:
            return self._reject("daily_loss_limit")
        for v in (equity, price, stop_distance):
            if not math.isfinite(v) or v <= 0:
                return self._reject("invalid_input")
        if open_positions >= self.cfg.max_open_positions:
            return self._reject("max_open_positions")
        qty = self.risk_amount(equity) / stop_distance
        qty = min(qty, equity * self.cfg.max_leverage / price)  # leverage cap
        qty = self.instrument.round_qty(qty)
        if qty < self.instrument.min_qty:
            return self._reject("below_min_size")
        return Decision(True, qty, "ok")
