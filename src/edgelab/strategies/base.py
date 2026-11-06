from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

import pandas as pd


class Strategy(ABC):
    """A strategy turns OHLC candles into a desired position per bar.

    `signals(df)` returns a DataFrame aligned to df.index with:
      direction      +1 long, -1 short, 0 flat  (value known at that bar's CLOSE)
      stop_distance  price distance for the protective stop

    HARD RULE: the value at row i may only use data from rows <= i. The engine
    acts on it at the NEXT bar's open. tests/test_strategy_causality.py enforces
    this for every registered strategy.
    """

    name: ClassVar[str]
    defaults: ClassVar[dict] = {}

    def __init__(self, **params):
        unknown = set(params) - set(self.defaults)
        if unknown:
            raise ValueError(f"Unknown parameters for {self.name}: {sorted(unknown)}")
        self.params = {**self.defaults, **params}

    @property
    @abstractmethod
    def warmup_bars(self) -> int:
        """Bars needed before signals are trustworthy (indicator convergence)."""

    @abstractmethod
    def signals(self, df: pd.DataFrame) -> pd.DataFrame: ...
