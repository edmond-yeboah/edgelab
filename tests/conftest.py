import numpy as np
import pandas as pd
import pytest

from edgelab.models import CostModel, Instrument
from edgelab.risk import RiskConfig
from edgelab.strategies.base import Strategy

# A tiny instrument whose minimum size never blocks tests.
TEST_INSTRUMENT = Instrument("TEST", "fx", min_qty=1.0, qty_step=1.0)
NO_COST = CostModel()
LOOSE_RISK = RiskConfig(risk_per_trade_pct=100.0, daily_loss_limit_pct=100.0,  # big size so min_qty never blocks
                        max_drawdown_halt_pct=100.0, max_leverage=1_000.0)


def make_df(opens, highs=None, lows=None, closes=None, start="2024-01-01", freq="1h"):
    n = len(opens)
    closes = closes if closes is not None else opens
    highs = highs if highs is not None else [max(o, c) for o, c in zip(opens, closes)]
    lows = lows if lows is not None else [min(o, c) for o, c in zip(opens, closes)]
    idx = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes}, index=idx, dtype=float)


class Scripted(Strategy):
    """Test strategy returning a fixed direction/stop per bar."""
    name = "scripted"
    defaults = {}

    def __init__(self, directions, stop_distance):
        super().__init__()
        self._d = list(directions)
        self._s = stop_distance

    @property
    def warmup_bars(self) -> int:
        return 0

    def signals(self, df):
        return pd.DataFrame({"direction": self._d, "stop_distance": float(self._s)}, index=df.index)


@pytest.fixture
def scripted():
    return Scripted
