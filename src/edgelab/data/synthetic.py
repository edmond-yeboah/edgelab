"""Synthetic candles for TESTING THE PIPELINE ONLY. Never use these to judge a strategy.

Two generators with known properties make powerful sanity checks:
  * random_walk: no edge exists by construction. A sound pipeline must NOT find one.
  * trending:    persistent drift regimes. A trend strategy should profit before costs.
If the pipeline "discovers" an edge in the random walk, it is broken.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _to_ohlc(close: np.ndarray, rng: np.random.Generator, vol: float, start: str, freq: str) -> pd.DataFrame:
    n = len(close)
    open_ = np.concatenate([[close[0]], close[:-1]])
    wiggle = np.abs(rng.normal(0, vol * 0.5, n))
    high = np.maximum(open_, close) * (1 + wiggle)
    low = np.minimum(open_, close) * (1 - wiggle)
    idx = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": 1.0}, index=idx)


def random_walk(n: int = 30_000, seed: int = 0, start_price: float = 1.10, vol: float = 0.0004,
                start: str = "2020-01-01", freq: str = "1h") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = start_price * np.exp(np.cumsum(rng.normal(0, vol, n)))
    return _to_ohlc(close, rng, vol, start, freq)


def trending(n: int = 30_000, seed: int = 0, start_price: float = 1.10, vol: float = 0.0004,
             drift: float = 0.00012, mean_regime_bars: int = 400,
             start: str = "2020-01-01", freq: str = "1h") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    drifts = np.empty(n)
    i, sign = 0, 1
    while i < n:
        length = max(20, int(rng.exponential(mean_regime_bars)))
        drifts[i: i + length] = sign * drift
        sign = -sign if rng.random() < 0.7 else sign
        i += length
    close = start_price * np.exp(np.cumsum(drifts + rng.normal(0, vol, n)))
    return _to_ohlc(close, rng, vol, start, freq)
