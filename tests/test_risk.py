import pandas as pd

from edgelab.backtest import run_backtest
from edgelab.models import EURUSD, BTCUSD
from edgelab.risk import RiskConfig, RiskEngine
from conftest import NO_COST, TEST_INSTRUMENT, Scripted, make_df

TS = pd.Timestamp("2024-01-01", tz="UTC")


def test_position_size_matches_risk_budget():
    # $500 account, 0.5% risk = $2.50; stop 0.0010 -> 2500 units, floored to 2000 (1000-unit step)
    eng = RiskEngine(RiskConfig(risk_per_trade_pct=0.5, max_leverage=50), EURUSD)
    d = eng.size(equity=500, price=1.10, stop_distance=0.0010, open_positions=0)
    assert d.approved and d.qty == 2000
    assert d.qty * 0.0010 <= 2.5   # never risks MORE than the budget (rounded down)


def test_leverage_cap_blocks_micro_lot_on_small_account_at_1x():
    # Real finding for a $500 account: at 1:1 leverage one EURUSD micro lot (~$1,100 notional) is impossible.
    eng = RiskEngine(RiskConfig(risk_per_trade_pct=1.0, max_leverage=1.0), EURUSD)
    d = eng.size(500, 1.10, 0.0020, 0)
    assert not d.approved and d.reason == "below_min_size"
    eng5 = RiskEngine(RiskConfig(risk_per_trade_pct=1.0, max_leverage=5.0), EURUSD)
    assert eng5.size(500, 1.10, 0.0020, 0).approved


def test_crypto_spot_unleveraged_sizing():
    eng = RiskEngine(RiskConfig(risk_per_trade_pct=1.0, max_leverage=1.0), BTCUSD)
    d = eng.size(500, 60_000, 1_500, 0)          # risk $5 / $1500 stop = 0.0033 BTC, notional $200 < $500
    assert d.approved and abs(d.qty - 0.0033) < 1e-9


def test_invalid_inputs_fail_closed():
    eng = RiskEngine(RiskConfig(), EURUSD)
    for equity, price, stop in [(0, 1.1, 0.001), (500, 1.1, 0), (500, float("nan"), 0.001), (500, 1.1, float("nan")), (-5, 1.1, 0.001)]:
        d = eng.size(equity, price, stop, 0)
        assert not d.approved and d.reason == "invalid_input"


def test_max_open_positions_enforced():
    eng = RiskEngine(RiskConfig(max_open_positions=1), EURUSD)
    assert eng.size(500, 1.1, 0.002, open_positions=1).reason == "max_open_positions"


def test_daily_loss_limit_halts_then_resets_next_day():
    eng = RiskEngine(RiskConfig(daily_loss_limit_pct=2.0), EURUSD)
    eng.update(TS, 500)
    eng.update(TS + pd.Timedelta(hours=5), 489)         # -2.2% on the day
    assert eng.daily_halt and eng.size(489, 1.1, 0.002, 0).reason == "daily_loss_limit"
    eng.update(TS + pd.Timedelta(days=1), 489)           # new day
    assert not eng.daily_halt


def test_drawdown_trips_kill_switch_and_stays_tripped():
    eng = RiskEngine(RiskConfig(max_drawdown_halt_pct=10.0, daily_loss_limit_pct=100), EURUSD)
    eng.update(TS, 500)
    eng.update(TS + pd.Timedelta(days=1), 440)           # -12%
    assert eng.kill_switch
    eng.update(TS + pd.Timedelta(days=2), 600)           # recovery does NOT re-arm
    assert eng.kill_switch and eng.size(600, 1.1, 0.002, 0).reason == "kill_switch"
    eng.reset_kill_switch()                               # only a manual, explicit reset re-arms
    assert not eng.kill_switch


def test_kill_switch_flattens_open_position_in_backtest():
    # Long trade bleeds >10% of equity; kill switch must flatten and block re-entry.
    n = 12
    opens = [1000.0] * 3 + [1000 - 30 * k for k in range(1, n - 2)]
    df = make_df(opens=opens, closes=opens)
    # risk 50% of $1000 / stop 100 = 5 units; a 30-point drop per bar costs $150 (15%) per bar
    cfg = RiskConfig(risk_per_trade_pct=50.0, max_drawdown_halt_pct=10.0, daily_loss_limit_pct=100.0, max_leverage=10.0)
    res = run_backtest(df, Scripted([1] * n, 100.0), TEST_INSTRUMENT, NO_COST, cfg, 1000.0)
    assert any(e["event"] == "kill_switch" for e in res.risk_events)
    assert res.trades.iloc[-1]["exit_reason"] in ("kill_switch", "end_of_data")
    assert res.trades.iloc[0]["exit_reason"] == "kill_switch"
