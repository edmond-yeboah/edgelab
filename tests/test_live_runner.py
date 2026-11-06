"""The live/demo loop must make the SAME decisions as the validated backtester.

If ReplaySource + DemoRunner + PaperBroker reproduces run_backtest trade-for-trade,
then the live decision path is trustworthy (it's literally the same engine).
"""

import numpy as np
import pandas as pd
import pytest

from edgelab.backtest import run_backtest
from edgelab.brokers.paper import PaperBroker
from edgelab.config import load_config
from edgelab.data.synthetic import trending, random_walk
from edgelab.live.runner import DemoRunner, RunnerConfig
from edgelab.live.sources import ReplaySource
from edgelab.models import EURUSD
from edgelab.strategies import make_strategy


def _run_live(df, strat_name, params, cost, risk, equity0):
    strat = make_strategy(strat_name, **params)
    source = ReplaySource(df, start_index=1)
    broker = PaperBroker(cost, equity0)
    runner = DemoRunner(source, broker, strat, risk, EURUSD,
                        cfg=RunnerConfig(lookback=len(df), reentry_cooldown_bars=1))
    runner.run()
    return pd.DataFrame(broker.trades)


@pytest.mark.parametrize("gen,seed", [(trending, 5), (random_walk, 11), (trending, 2)])
def test_live_runner_matches_backtest(gen, seed):
    df = gen(n=8000, seed=seed)
    _, cost, _ = load_config()
    risk = type("R", (), {})  # placeholder to avoid confusion; use real config below
    from edgelab.risk import RiskConfig
    risk = RiskConfig(risk_per_trade_pct=1.0, max_leverage=20.0)
    params = {"fast": 20, "slow": 100}

    bt = run_backtest(df, make_strategy("ema_trend", **params), EURUSD, cost, risk, 500.0,
                      trade_from=0, reentry_cooldown_bars=1)
    live = _run_live(df, "ema_trend", params, cost, risk, 500.0)

    assert len(bt.trades) == len(live), f"trade count differs: bt={len(bt.trades)} live={len(live)}"
    if len(bt.trades):
        for col in ["direction", "entry_price", "exit_price", "pnl"]:
            np.testing.assert_allclose(bt.trades[col].to_numpy(float), live[col].to_numpy(float),
                                       rtol=1e-9, atol=1e-9, err_msg=f"{col} mismatch")
        assert list(bt.trades["entry_time"]) == list(live["entry_time"])
        assert list(bt.trades["exit_reason"]) == list(live["exit_reason"])


def test_runner_rejects_lookback_below_warmup():
    df = trending(n=1000, seed=1)
    from edgelab.risk import RiskConfig
    _, cost, _ = load_config()
    strat = make_strategy("ema_trend")   # warmup = 300
    with pytest.raises(ValueError, match="lookback"):
        DemoRunner(ReplaySource(df), PaperBroker(cost, 500.0), strat,
                   RiskConfig(), EURUSD, cfg=RunnerConfig(lookback=100))


def test_runner_fails_closed_on_live_account_mismatch():
    from edgelab.mode import ModeError
    from edgelab.brokers.base import Broker
    from edgelab.mode import ExecutionMode
    df = trending(n=1000, seed=1)
    _, cost, _ = load_config()

    class FakeLiveBroker(PaperBroker):
        is_simulated = False
        mode = ExecutionMode.DEMO      # says demo ...

    from edgelab.risk import RiskConfig
    # ... but the account is live -> must refuse to start
    with pytest.raises(ModeError):
        DemoRunner(ReplaySource(df), FakeLiveBroker(cost, 500.0), make_strategy("ema_trend"),
                   RiskConfig(), EURUSD, cfg=RunnerConfig(lookback=1000), account_is_live=True)
