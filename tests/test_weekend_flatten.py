"""Backtester option: flatten before a market-closure gap, re-enter after. Default = hold."""

import pandas as pd

from edgelab.backtest import run_backtest
from edgelab.config import load_config
from edgelab.data.synthetic import trending
from edgelab.models import EURUSD
from edgelab.risk import RiskConfig
from edgelab.strategies import make_strategy

RISK = RiskConfig(risk_per_trade_pct=0.5, daily_loss_limit_pct=100.0, max_drawdown_halt_pct=100.0,
                  max_leverage=500.0)


def _h4(weekends: bool) -> pd.DataFrame:
    df = trending(n=6000, seed=3, freq="4h", vol=0.003, drift=0.0008)
    if not weekends:                       # FX-style: no Saturday/Sunday bars -> a ~2-day gap
        df = df[df.index.dayofweek < 5]
    return df


def _run(df, cost=None, **kw):
    if cost is None:
        _, cost, _ = load_config()
    strat = make_strategy("ema_trend", fast=30, slow=150, atr_mult=2.0)
    return run_backtest(df, strat, EURUSD, cost, RISK, 1_000_000.0, reentry_cooldown_bars=1, **kw)


def test_default_holds_through_weekends():
    res = _run(_h4(weekends=False))
    assert len(res.trades) > 5
    assert "weekend_flat" not in set(res.trades["exit_reason"])


def test_flatten_closes_on_the_last_bar_before_each_gap():
    df = _h4(weekends=False)
    res = _run(df, flatten_gap_hours=24)
    flat = res.trades[res.trades["exit_reason"] == "weekend_flat"]
    assert len(flat) > 3
    nxt = pd.Series(df.index[1:], index=df.index[:-1])
    for t in flat["exit_time"]:
        assert nxt[t] - t > pd.Timedelta(hours=24)         # only ever fires right before a gap
        assert t.dayofweek == 4                            # Friday's last bar


def test_flatten_is_a_noop_when_the_market_never_closes():
    df = _h4(weekends=True)                                # continuous (crypto-like)
    a, b = _run(df), _run(df, flatten_gap_hours=24)
    assert "weekend_flat" not in set(b.trades["exit_reason"])
    pd.testing.assert_frame_equal(a.trades.reset_index(drop=True), b.trades.reset_index(drop=True))


def test_flatten_adds_trades_and_costs_vs_hold():
    from edgelab.models import CostModel
    cost = CostModel(spread_bps=1.0, fee_bps_per_side=1.0, slippage_bps_per_side=0.2)
    df = _h4(weekends=False)
    hold, flat = _run(df, cost), _run(df, cost, flatten_gap_hours=24)
    assert len(flat.trades) > len(hold.trades)             # extra exits (and re-entries)
    assert flat.trades["fees"].sum() > hold.trades["fees"].sum()
