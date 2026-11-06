"""Market-data sources for the demo/live runner.

A source yields *closed* candles. The runner never acts on a forming bar, which
is the live-trading equivalent of the backtester's "act at the next open".
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from ..data.storage import validate_candles


class MarketDataSource(ABC):
    @abstractmethod
    def history(self, lookback: int) -> pd.DataFrame:
        """Return the most recent `lookback` CLOSED candles (UTC index, OHLC)."""

    @abstractmethod
    def wait_next_closed(self) -> pd.Timestamp | None:
        """Block until a new candle closes; return its timestamp, or None when exhausted."""


class ReplaySource(MarketDataSource):
    """Replays a stored DataFrame one closed bar at a time. Used for tests and offline dry-runs.

    It exposes exactly the data a live source would: at step i, history() returns
    bars 0..i inclusive (all closed), and wait_next_closed advances to i+1.
    """

    def __init__(self, df: pd.DataFrame, start_index: int = 1):
        validate_candles(df)
        self.df = df
        self._i = max(0, start_index - 1)
        self._started = False

    def history(self, lookback: int) -> pd.DataFrame:
        hi = self._i + 1
        lo = max(0, hi - lookback)
        return self.df.iloc[lo:hi]

    def wait_next_closed(self) -> pd.Timestamp | None:
        if not self._started:
            self._started = True
            return self.df.index[self._i]
        if self._i + 1 >= len(self.df):
            return None
        self._i += 1
        return self.df.index[self._i]


class CTraderSource(MarketDataSource):
    """Live closed trendbars from cTrader Open API.

    UNVERIFIED against live servers from this build. The wiring is documented in
    SETUP_CTRADER.md; `client` is a connected CTraderClient (see brokers/ctrader.py).
    """

    def __init__(self, client, symbol: str, timeframe: str = "H1"):
        self.client = client
        self.symbol = symbol
        self.timeframe = timeframe

    def history(self, lookback: int) -> pd.DataFrame:
        from .runner import _period_delta  # local import to avoid cycle
        df = self.client.get_trendbars(self.symbol, self.timeframe, lookback + 1)
        if len(df):
            period = _period_delta(self.timeframe)
            forming_start = pd.Timestamp.utcnow().floor(period)
            # drop only a still-forming final bar (its start == current period), keep closed bars
            if df.index[-1] >= forming_start:
                df = df.iloc[:-1]
        return df.tail(lookback)

    def wait_next_closed(self) -> pd.Timestamp | None:
        return self.client.wait_next_closed_bar(self.symbol, self.timeframe)
