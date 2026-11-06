from __future__ import annotations

import numpy as np
import pandas as pd

from .base import Strategy


class EmaTrend(Strategy):
    """Baseline: long when the fast EMA is above the slow EMA, short when below.

    This is a HYPOTHESIS to test, not a proven edge. Its job is to be simple and
    transparent so the pipeline (costs, validation, risk) can be trusted before
    anything cleverer is added.
    """

    name = "ema_trend"
    defaults = {"fast": 20, "slow": 100, "atr_n": 14, "atr_mult": 3.0}

    def __init__(self, **params):
        super().__init__(**params)
        if self.params["fast"] >= self.params["slow"]:
            raise ValueError("fast must be smaller than slow")

    @property
    def warmup_bars(self) -> int:
        return int(max(self.params["slow"], self.params["atr_n"]) * 3)

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        c, h, l = df["close"], df["high"], df["low"]
        fast = c.ewm(span=p["fast"], adjust=False).mean()
        slow = c.ewm(span=p["slow"], adjust=False).mean()
        prev_c = c.shift(1)
        tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1.0 / p["atr_n"], adjust=False).mean()
        direction = pd.Series(np.sign(fast - slow), index=df.index).fillna(0).astype(int)
        direction.iloc[: self.warmup_bars] = 0
        return pd.DataFrame({"direction": direction, "stop_distance": atr * p["atr_mult"]}, index=df.index)
