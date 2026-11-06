from __future__ import annotations

import numpy as np
import pandas as pd

from .base import Strategy


class BollingerReversion(Strategy):
    """Fade stretched moves: go long when price closes below the lower band, short above the upper.

    A different HYPOTHESIS from the trend filter (and one that classically struggles in trending FX),
    which is exactly why it's useful: the validation pipeline should be able to tell them apart on
    real data rather than us assuming either works. Exit is handled by the stop or an opposite signal;
    entries flat inside the band.
    """

    name = "bollinger_reversion"
    defaults = {"lookback": 50, "n_std": 2.0, "atr_n": 14, "atr_mult": 2.0}

    @property
    def warmup_bars(self) -> int:
        return int(max(self.params["lookback"], self.params["atr_n"]) * 3)

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        c, h, l = df["close"], df["high"], df["low"]
        ma = c.rolling(p["lookback"]).mean()
        sd = c.rolling(p["lookback"]).std(ddof=0)
        upper, lower = ma + p["n_std"] * sd, ma - p["n_std"] * sd
        prev_c = c.shift(1)
        tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1.0 / p["atr_n"], adjust=False).mean()

        direction = pd.Series(0, index=df.index, dtype=int)
        direction[c < lower] = 1     # stretched down -> expect bounce up
        direction[c > upper] = -1    # stretched up -> expect fade down
        direction.iloc[: self.warmup_bars] = 0
        return pd.DataFrame({"direction": direction, "stop_distance": atr * p["atr_mult"]}, index=df.index)
