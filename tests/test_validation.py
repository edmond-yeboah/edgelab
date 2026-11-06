import pandas as pd
import pytest

from edgelab.config import load_config
from edgelab.data.storage import DataError, validate_candles
from edgelab.data.synthetic import random_walk, trending
from edgelab.experiments import ExperimentLog
from edgelab.mode import ExecutionMode, ModeError, assert_account_matches_mode, resolve_mode
from edgelab.models import EURUSD
from edgelab.risk import RiskConfig
from edgelab.validation import (Criteria, HoldoutAlreadyUsed, check_acceptance, chrono_split,
                                dataset_id, evaluate_holdout, walk_forward)

RISK = RiskConfig(risk_per_trade_pct=1.0, max_leverage=20.0)   # sized so micro lots are tradable on $500
GRID = {"fast": [10, 20], "slow": [60, 100]}


def test_acceptance_gates_pass_and_fail():
    good = {"n_trades": 300, "profit_factor": 1.6, "max_drawdown_pct": 9.0, "expectancy_r": 0.2}
    ok_stress = {"profit_factor": 1.2}
    assert check_acceptance(good, ok_stress, Criteria()).passed
    assert not check_acceptance({**good, "n_trades": 50}, ok_stress, Criteria()).passed
    assert not check_acceptance(good, {"profit_factor": 0.8}, Criteria()).passed
    assert not check_acceptance({**good, "max_drawdown_pct": 30}, ok_stress, Criteria()).passed


def test_holdout_can_only_be_used_once(tmp_path):
    _, cost, crit = load_config()
    df = trending(n=6000, seed=2)
    dev, hold = chrono_split(df, 0.2)
    ds = dataset_id("EURUSD", "1h", df)
    args = dict(df=df, holdout_start=len(dev), strategy_name="ema_trend", params={}, instrument=EURUSD,
                cost=cost, risk_cfg=RISK, equity0=500.0, criteria=crit, state_dir=tmp_path, dataset=ds)
    evaluate_holdout(**args)
    with pytest.raises(HoldoutAlreadyUsed):
        evaluate_holdout(**args)


def test_pipeline_finds_no_edge_in_a_random_walk():
    """The most important sanity test: where no edge exists, validation must not invent one."""
    _, cost, crit = load_config()
    df = random_walk(n=20_000, seed=11)
    wf = walk_forward(df, "ema_trend", GRID, EURUSD, cost, RISK, 500.0, train_bars=4000, test_bars=2000)
    assert wf.metrics["n_trades"] > 20
    assert wf.metrics["profit_factor"] < crit.min_profit_factor or wf.metrics["expectancy_r"] < crit.min_expectancy_r


def test_pipeline_finds_the_edge_when_one_exists():
    """And the converse, so we know the pipeline isn't just always saying no."""
    _, cost, _ = load_config()
    df = trending(n=20_000, seed=5)
    wf = walk_forward(df, "ema_trend", GRID, EURUSD, cost, RISK, 500.0, train_bars=4000, test_bars=2000)
    assert wf.metrics["n_trades"] > 20
    assert wf.metrics["net_pnl"] > 0 and wf.metrics["profit_factor"] > 1.3


def test_walk_forward_logs_every_trial(tmp_path):
    _, cost, _ = load_config()
    df = random_walk(n=9000, seed=1)
    log = ExperimentLog(tmp_path / "log.jsonl")
    walk_forward(df, "ema_trend", GRID, EURUSD, cost, RISK, 500.0, train_bars=4000, test_bars=2000, log=log, dataset="d")
    windows = 2   # (9000 - 4000) // 2000
    assert log.n_trials("d") == windows * 4   # every param combo tried is recorded


def test_candle_validator_catches_bad_data():
    good = random_walk(n=200)
    validate_candles(good)
    dup = pd.concat([good, good.iloc[:1]]).sort_index()
    with pytest.raises(DataError):
        validate_candles(dup)
    nan = good.copy(); nan.iloc[5, 0] = float("nan")
    with pytest.raises(DataError):
        validate_candles(nan)
    bad_hl = good.copy(); bad_hl.iloc[10, bad_hl.columns.get_loc("high")] = 0.5
    with pytest.raises(DataError):
        validate_candles(bad_hl)
    naive = good.copy(); naive.index = naive.index.tz_localize(None)
    with pytest.raises(DataError):
        validate_candles(naive)


def test_mode_defaults_to_demo_and_fails_closed():
    assert resolve_mode(None) is ExecutionMode.DEMO
    assert resolve_mode("demo") is ExecutionMode.DEMO
    with pytest.raises(ModeError):
        resolve_mode("live", env={})
    with pytest.raises(ModeError):
        resolve_mode("paper-ish", env={})
    assert resolve_mode("live", env={"EDGELAB_ALLOW_LIVE": "I_UNDERSTAND_THE_RISK"}) is ExecutionMode.LIVE
    with pytest.raises(ModeError):
        assert_account_matches_mode(ExecutionMode.DEMO, account_is_live=True)
    with pytest.raises(ModeError):
        assert_account_matches_mode(ExecutionMode.DEMO, account_is_live=None)
    assert_account_matches_mode(ExecutionMode.DEMO, account_is_live=False)
