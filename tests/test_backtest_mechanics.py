import pandas as pd

from edgelab.backtest import run_backtest
from edgelab.models import CostModel
from conftest import LOOSE_RISK, NO_COST, TEST_INSTRUMENT, Scripted, make_df


def run(df, directions, stop, cost=NO_COST, risk=LOOSE_RISK, equity0=1000.0):
    return run_backtest(df, Scripted(directions, stop), TEST_INSTRUMENT, cost, risk, equity0)


def test_entry_happens_at_next_bar_open_not_signal_bar():
    # Signal (long) is known at the close of bar 1; entry must be at bar 2's OPEN (=105).
    df = make_df(opens=[100, 100, 105, 106, 107], closes=[100, 100, 106, 107, 108])
    res = run(df, [0, 1, 1, 1, 1], stop=50)
    t = res.trades.iloc[0]
    assert t["entry_time"] == df.index[2]
    assert t["entry_price"] == 105.0   # not 100 (signal bar close) -> no look-ahead


def test_signal_exit_fills_at_next_open():
    df = make_df(opens=[100, 100, 101, 110, 120, 125], closes=[100, 100, 101, 110, 120, 125])
    res = run(df, [1, 1, 0, 0, 0, 0], stop=50)   # flat signal at close of bar 2 -> exit at open of bar 3
    t = res.trades.iloc[0]
    assert t["exit_time"] == df.index[3]
    assert t["exit_price"] == 110.0
    assert t["exit_reason"] == "signal_exit"


def test_stop_fills_at_stop_price_intrabar():
    # Long entered at bar1 open=100 with stop 5 -> stop=95. Bar 2 trades down to 90 within the bar.
    df = make_df(opens=[100, 100, 99, 98], highs=[100, 101, 100, 99], lows=[100, 99, 90, 97],
                 closes=[100, 100, 91, 98])
    res = run(df, [1, 1, 1, 1], stop=5)
    t = res.trades.iloc[0]
    assert t["exit_reason"] == "stop"
    assert t["exit_price"] == 95.0


def test_gap_through_stop_fills_at_open_not_stop():
    # Stop=95 but bar 2 opens at 85: the fill must be at 85 (can't get 95).
    df = make_df(opens=[100, 100, 85, 85], closes=[100, 100, 85, 85])
    res = run(df, [1, 1, 1, 1], stop=5)
    t = res.trades.iloc[0]
    assert t["exit_reason"] == "stop_gap"
    assert t["exit_price"] == 85.0


def test_no_same_bar_reentry_after_stop():
    # Stopped out on bar 2; with the default 1-bar cooldown there must be no re-entry on bar 2,
    # and (prices flat afterwards) none while flat -> exactly one trade here.
    opens = [100, 100, 99, 98, 98]
    lows = [100, 99, 90, 97, 97]
    df = make_df(opens=opens, lows=lows, closes=opens)
    res = run(df, [1] * 5, stop=5)
    assert list(res.trades.exit_reason) == ["stop"]


def test_trend_reentry_after_cooldown():
    # The real fix: a long stops out on a dip, the uptrend resumes, and the bot must be able to
    # re-join it after the cooldown even though the signal never left +1. (Old code stranded it.)
    from edgelab.risk import RiskConfig
    gentle = RiskConfig(risk_per_trade_pct=1.0, daily_loss_limit_pct=100.0,
                        max_drawdown_halt_pct=100.0, max_leverage=1000.0)
    opens = [100, 100, 101, 99, 101, 103, 105, 107, 109, 111]
    lows = [100, 99, 100, 90, 100, 102, 104, 106, 108, 110]
    df = make_df(opens=opens, lows=lows, closes=opens)
    res = run(df, [1] * 10, stop=5, risk=gentle)
    assert len(res.trades) >= 2                    # stopped once, then back in the trend
    assert res.trades.iloc[0]["exit_reason"] == "stop"
    # cooldown=0 would allow re-entry on the very next bar after the stop; default is 1 bar later.
    stop_bar = res.trades.iloc[0]["exit_time"]
    second_entry = res.trades.iloc[1]["entry_time"]
    assert second_entry > stop_bar


def test_nonfinite_ohlc_is_rejected():
    import pytest
    df = make_df(opens=[100, 101, 102, 103], closes=[100, 101, 102, 103])
    df.iloc[2, df.columns.get_loc("high")] = float("nan")
    with pytest.raises(ValueError, match="non-finite"):
        run(df, [1, 1, 1, 1], stop=50)


def test_costs_reduce_net_pnl_monotonically():
    opens = [100, 100, 101, 102, 103, 104, 105, 106]
    df = make_df(opens=opens, closes=opens)
    d = [1, 1, 1, 1, 1, 1, 1, 0]
    free = run(df, d, 50, NO_COST).metrics["net_pnl"]
    spread = run(df, d, 50, CostModel(spread_bps=20)).metrics["net_pnl"]
    spread_fee = run(df, d, 50, CostModel(spread_bps=20, fee_bps_per_side=10)).metrics["net_pnl"]
    assert free > spread > spread_fee


def test_short_pnl_sign():
    opens = [100, 100, 98, 96, 94]
    df = make_df(opens=opens, closes=opens)
    res = run(df, [-1, -1, -1, 0, 0], stop=50)
    assert res.trades.iloc[0]["pnl"] > 0


def test_stress_cost_can_flip_a_marginal_edge():
    # Tiny gross edge: wiped out once costs are doubled.
    opens = [100, 100, 100.05, 100.05, 100.05]
    df = make_df(opens=opens, closes=opens)
    d = [1, 1, 0, 0, 0]
    base = CostModel(spread_bps=2)
    assert run(df, d, 50, base).metrics["net_pnl"] > 0
    assert run(df, d, 50, base.scaled(5)).metrics["net_pnl"] < 0
