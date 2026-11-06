"""More standard 'market analysis' strategies — each a hypothesis to test, not a known edge.

These are the indicators people reach for (RSI, MACD, Donchian breakout). They get the SAME
honest treatment as everything else: causal signals, costs, walk-forward, cost-stress, holdout.
Adding an indicator does not add an edge; the validator decides.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .base import Strategy


def _atr(df: pd.DataFrame, n: int) -> pd.Series:
    c, h, l = df["close"], df["high"], df["low"]
    prev = c.shift(1)
    tr = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False).mean()


class RsiReversion(Strategy):
    """Fade extremes: long when RSI < oversold, short when RSI > overbought."""
    name = "rsi_reversion"
    defaults = {"n": 14, "oversold": 30, "overbought": 70, "atr_n": 14, "atr_mult": 2.0}

    @property
    def warmup_bars(self) -> int:
        return int(max(self.params["n"], self.params["atr_n"]) * 5)

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        delta = df["close"].diff()
        gain = delta.clip(lower=0).ewm(alpha=1.0 / p["n"], adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1.0 / p["n"], adjust=False).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = 100 - 100 / (1 + rs)
        direction = pd.Series(0, index=df.index, dtype=int)
        direction[rsi < p["oversold"]] = 1
        direction[rsi > p["overbought"]] = -1
        direction.iloc[: self.warmup_bars] = 0
        return pd.DataFrame({"direction": direction, "stop_distance": _atr(df, p["atr_n"]) * p["atr_mult"]},
                            index=df.index)


class MacdTrend(Strategy):
    """Momentum: long when the MACD line is above its signal line, short when below."""
    name = "macd_trend"
    defaults = {"fast": 12, "slow": 26, "signal": 9, "atr_n": 14, "atr_mult": 3.0}

    def __init__(self, **params):
        super().__init__(**params)
        if self.params["fast"] >= self.params["slow"]:
            raise ValueError("fast must be < slow")

    @property
    def warmup_bars(self) -> int:
        return int((self.params["slow"] + self.params["signal"]) * 4)

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        c = df["close"]
        macd = c.ewm(span=p["fast"], adjust=False).mean() - c.ewm(span=p["slow"], adjust=False).mean()
        sig = macd.ewm(span=p["signal"], adjust=False).mean()
        direction = pd.Series(np.sign(macd - sig), index=df.index).fillna(0).astype(int)
        direction.iloc[: self.warmup_bars] = 0
        return pd.DataFrame({"direction": direction, "stop_distance": _atr(df, p["atr_n"]) * p["atr_mult"]},
                            index=df.index)


class DonchianBreakout(Strategy):
    """Classic breakout: long on a new N-bar high, short on a new N-bar low (uses only prior bars)."""
    name = "donchian_breakout"
    defaults = {"n": 55, "atr_n": 14, "atr_mult": 3.0}

    @property
    def warmup_bars(self) -> int:
        return int(max(self.params["n"], self.params["atr_n"]) * 3)

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        # channels from the PRIOR n bars (shift(1)) so the current bar isn't used to define its own breakout
        upper = df["high"].rolling(p["n"]).max().shift(1)
        lower = df["low"].rolling(p["n"]).min().shift(1)
        direction = pd.Series(0, index=df.index, dtype=int)
        direction[df["close"] > upper] = 1
        direction[df["close"] < lower] = -1
        direction.iloc[: self.warmup_bars] = 0
        return pd.DataFrame({"direction": direction, "stop_distance": _atr(df, p["atr_n"]) * p["atr_mult"]},
                            index=df.index)
