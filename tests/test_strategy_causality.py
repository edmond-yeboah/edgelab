"""No look-ahead: a signal at bar i must not change when FUTURE bars are added."""

import numpy as np
import pandas as pd
import pytest

from edgelab.data.synthetic import trending
from edgelab.strategies import STRATEGIES, make_strategy


@pytest.mark.parametrize("name", sorted(STRATEGIES))
def test_signals_are_causal(name):
    df = trending(n=1500, seed=3)
    strat = make_strategy(name)
    full = strat.signals(df)
    for cut in (400, 777, 1200):
        prefix = strat.signals(df.iloc[:cut])
        a = prefix.iloc[-50:].to_numpy(dtype=float)
        b = full.iloc[cut - 50: cut].to_numpy(dtype=float)
        np.testing.assert_allclose(a, b, equal_nan=True, err_msg=f"{name} uses future data (cut={cut})")


@pytest.mark.parametrize("name", sorted(STRATEGIES))
def test_signals_flat_during_warmup(name):
    df = trending(n=1000, seed=1)
    strat = make_strategy(name)
    s = strat.signals(df)
    assert (s["direction"].iloc[: strat.warmup_bars] == 0).all()
