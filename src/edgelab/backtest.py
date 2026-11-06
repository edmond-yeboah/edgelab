"""Bar-by-bar backtester built on the SAME engine and broker interface as demo trading.

Timeline per bar i:
  1. begin_bar: the bar opens (gap-stops are handled here)
  2. engine.on_signal uses the signal computed at the CLOSE of bar i-1  -> no look-ahead
  3. end_bar: intrabar stops are checked, equity is marked at the close
  4. risk limits are updated
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import metrics as M
from .brokers.paper import PaperBroker
from .engine import TradingEngine
from .models import CostModel, Instrument
from .risk import RiskConfig, RiskEngine
from .strategies.base import Strategy

REQUIRED_COLUMNS = ["open", "high", "low", "close"]


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    equity: pd.Series
    metrics: dict
    rejections: dict = field(default_factory=dict)
    risk_events: list = field(default_factory=list)


def run_backtest(
    df: pd.DataFrame,
    strategy: Strategy,
    instrument: Instrument,
    cost: CostModel,
    risk_cfg: RiskConfig,
    equity0: float = 500.0,
    trade_from: int = 0,
    reentry_cooldown_bars: int = 1,
) -> BacktestResult:
    """Run `strategy` over `df`. Bars before `trade_from` are warm-up only (no trading)."""
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Candles missing columns: {missing}")
    if not np.isfinite(df[REQUIRED_COLUMNS].to_numpy(dtype=float)).all():
        raise ValueError("OHLC contains non-finite values; run data.storage.validate_candles first.")

    sig = strategy.signals(df)
    dirs = sig["direction"].fillna(0).to_numpy(dtype=int)
    stops = sig["stop_distance"].to_numpy(dtype=float)
    o, h, l, c = (df[k].to_numpy(dtype=float) for k in REQUIRED_COLUMNS)
    idx = df.index

    broker = PaperBroker(cost, equity0)
    risk = RiskEngine(risk_cfg, instrument)
    engine = TradingEngine(broker, risk, reentry_cooldown_bars=reentry_cooldown_bars)

    for i in range(len(df)):
        broker.begin_bar(idx[i], o[i], h[i], l[i], c[i])
        if i > 0 and i >= trade_from:
            engine.on_signal(idx[i], int(dirs[i - 1]), float(stops[i - 1]))
        broker.end_bar()
        engine.after_bar(idx[i])

    broker.force_close_at(c[-1], "end_of_data")
    broker.pop_events()

    trades = pd.DataFrame(broker.trades)
    equity = pd.Series([e for _, e in broker.equity_curve], index=[t for t, _ in broker.equity_curve])
    return BacktestResult(
        trades=trades,
        equity=equity,
        metrics=M.compute(trades, equity0, equity.iloc[trade_from:] if len(equity) > trade_from else None),
        rejections=dict(risk.rejections),
        risk_events=risk.events,
    )
