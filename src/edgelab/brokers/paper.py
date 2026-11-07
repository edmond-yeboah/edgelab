"""Simulated broker used by the backtester and for offline demo runs.

Fill rules (deliberately conservative):
  * market orders fill at the bar OPEN, worse by half-spread + slippage
  * stops trigger when the bar's low/high touches them and fill at the stop,
    worse by half-spread + slippage
  * if a bar opens beyond the stop (a gap) the fill is at the open, not the stop
  * exchange/broker fees are charged on notional at both entry and exit
"""

from __future__ import annotations

import pandas as pd

from ..mode import ExecutionMode
from ..models import CostModel
from .base import Broker, CloseEvent, Position


class PaperBroker(Broker):
    mode = ExecutionMode.DEMO
    is_simulated = True

    def __init__(self, cost: CostModel, starting_equity: float):
        self.cost = cost
        self.cash = float(starting_equity)
        self._pos: Position | None = None
        self._events: list[CloseEvent] = []
        self._realized: list[dict] = []
        self.trades: list[dict] = []
        self.equity_curve: list[tuple[pd.Timestamp, float]] = []
        self._ts: pd.Timestamp | None = None
        self._o = self._h = self._l = self._c = float("nan")
        self._last = float("nan")

    # ---- Broker interface ----------------------------------------------
    def position(self) -> Position | None:
        return self._pos

    def reference_price(self) -> float:
        return self._o

    def equity(self) -> float:
        if self._pos is None:
            return self.cash
        p = self._pos
        unreal = p.direction * (self._last - p.entry_price) * p.qty
        return self.cash + unreal - p.entry_fee

    def submit_market(self, direction: int, qty: float, stop_distance: float, risk_amount: float) -> Position:
        if self._pos is not None:
            raise RuntimeError("PaperBroker supports one position at a time")
        fill = self._o * (1 + direction * self.cost.adverse_fraction)
        fee = self.cost.fee_fraction * fill * qty
        self._pos = Position(
            direction=direction, qty=qty, entry_price=fill,
            stop=self._o - direction * stop_distance,
            entry_time=self._ts, risk_amount=risk_amount, entry_fee=fee,
        )
        return self._pos

    def close_position(self, reason: str) -> None:
        if self._pos is not None:
            self._close(self._o, reason)

    def pop_events(self) -> list[CloseEvent]:
        ev, self._events = self._events, []
        return ev

    def pop_realized(self) -> list[dict]:
        r, self._realized = self._realized, []
        return r

    # ---- bar replay (used by the backtester) ---------------------------
    def begin_bar(self, ts: pd.Timestamp, o: float, h: float, l: float, c: float) -> None:
        self._ts, self._o, self._h, self._l, self._c = ts, o, h, l, c
        self._last = o
        p = self._pos
        if p is not None:  # gap through the stop at the open
            if (p.direction == 1 and o <= p.stop) or (p.direction == -1 and o >= p.stop):
                self._close(o, "stop_gap")

    def end_bar(self) -> None:
        p = self._pos
        if p is not None:
            if (p.direction == 1 and self._l <= p.stop) or (p.direction == -1 and self._h >= p.stop):
                self._close(p.stop, "stop")
        self._last = self._c
        self.equity_curve.append((self._ts, self.equity()))

    def force_close_at(self, price: float, reason: str) -> None:
        if self._pos is not None:
            self._close(price, reason)

    # ---- internals -------------------------------------------------------
    def _close(self, mid_price: float, reason: str) -> None:
        p = self._pos
        assert p is not None
        exit_px = mid_price * (1 - p.direction * self.cost.adverse_fraction)
        exit_fee = self.cost.fee_fraction * exit_px * p.qty
        gross = p.direction * (exit_px - p.entry_price) * p.qty
        fees = p.entry_fee + exit_fee
        net = gross - fees
        self.cash += net
        r_mult = net / p.risk_amount if p.risk_amount else float("nan")
        self.trades.append({
            "entry_time": p.entry_time, "exit_time": self._ts, "direction": p.direction,
            "qty": p.qty, "entry_price": p.entry_price, "exit_price": exit_px,
            "gross_pnl": gross, "fees": fees, "pnl": net,
            "r_multiple": r_mult,
            "exit_reason": reason,
        })
        self._realized.append({"pnl": round(net, 2), "reason": reason, "dir": p.direction, "r": r_mult})
        self._events.append(CloseEvent(p.direction, reason))
        self._pos = None
