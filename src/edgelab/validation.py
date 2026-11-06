"""Honest validation: walk-forward testing, a single-use holdout, cost stress, acceptance gates."""

from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import metrics as M
from .backtest import run_backtest
from .experiments import ExperimentLog, _clean
from .models import CostModel, Instrument
from .risk import RiskConfig
from .strategies import make_strategy


# ---- splitting ---------------------------------------------------------------
def chrono_split(df: pd.DataFrame, holdout_frac: float = 0.2) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split by time. `dev` is for ALL research and tuning; `holdout` is touched once."""
    if not 0 < holdout_frac < 0.5:
        raise ValueError("holdout_frac must be in (0, 0.5)")
    cut = int(len(df) * (1 - holdout_frac))
    return df.iloc[:cut], df.iloc[cut:]


def dataset_id(symbol: str, timeframe: str, df: pd.DataFrame) -> str:
    raw = f"{symbol}|{timeframe}|{df.index[0]}|{df.index[-1]}|{len(df)}"
    return f"{symbol}_{timeframe}_{hashlib.sha1(raw.encode()).hexdigest()[:8]}"


# ---- walk-forward ------------------------------------------------------------
@dataclass
class WalkForwardResult:
    trades: pd.DataFrame
    windows: list[dict]
    metrics: dict


def walk_forward(
    df: pd.DataFrame,
    strategy_name: str,
    param_grid: dict[str, list],
    instrument: Instrument,
    cost: CostModel,
    risk_cfg: RiskConfig,
    equity0: float,
    train_bars: int,
    test_bars: int,
    min_train_trades: int = 10,
    log: ExperimentLog | None = None,
    dataset: str = "",
) -> WalkForwardResult:
    """Pick parameters on each training window, then trade the NEXT unseen window with them.

    Only the out-of-sample (test) trades are returned, so the result is not
    flattered by in-sample fitting. Each test window restarts from `equity0`.
    """
    keys = list(param_grid)
    combos = [dict(zip(keys, v)) for v in itertools.product(*(param_grid[k] for k in keys))]
    windows: list[dict] = []
    oos: list[pd.DataFrame] = []
    start = 0
    while start + train_bars + test_bars <= len(df):
        train = df.iloc[start: start + train_bars]
        best, best_score = None, None
        for params in combos:
            strat = make_strategy(strategy_name, **params)
            res = run_backtest(train, strat, instrument, cost, risk_cfg, equity0, trade_from=strat.warmup_bars)
            if log:
                log.record("train", strategy_name, params, dataset, res.metrics, window_start=str(train.index[0]))
            if res.metrics["n_trades"] >= min_train_trades:
                score = res.metrics["expectancy_r"]
                if best_score is None or score > best_score:
                    best, best_score = params, score
        test_end = start + train_bars + test_bars
        if best is not None:
            strat = make_strategy(strategy_name, **best)
            wstart = max(0, start + train_bars - strat.warmup_bars)
            res = run_backtest(
                df.iloc[wstart:test_end], strat, instrument, cost, risk_cfg, equity0,
                trade_from=start + train_bars - wstart,
            )
            if len(res.trades):
                oos.append(res.trades)
            windows.append({"test_start": str(df.index[start + train_bars]), "params": best,
                            "train_expectancy_r": best_score, "oos": res.metrics})
        else:
            windows.append({"test_start": str(df.index[start + train_bars]), "params": None, "oos": M.compute(None, equity0)})
        start += test_bars
    trades = pd.concat(oos, ignore_index=True) if oos else pd.DataFrame()
    return WalkForwardResult(trades, windows, M.compute(trades, equity0))


# ---- acceptance gates --------------------------------------------------------
@dataclass(frozen=True)
class Criteria:
    min_trades: int = 200
    min_profit_factor: float = 1.3
    max_drawdown_pct: float = 15.0
    min_expectancy_r: float = 0.05
    cost_stress_multiplier: float = 2.0
    min_stressed_profit_factor: float = 1.0

    @classmethod
    def from_dict(cls, d: dict) -> "Criteria":
        allowed = {f.name for f in fields(cls)}
        unknown = set(d) - allowed
        if unknown:
            raise ValueError(f"Unknown acceptance keys: {sorted(unknown)}")
        return cls(**d)


@dataclass
class AcceptanceResult:
    passed: bool
    checks: list[tuple[str, bool, str]]

    def report(self) -> str:
        return "\n".join(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}" for name, ok, detail in self.checks)


def check_acceptance(oos: dict, stressed: dict, c: Criteria) -> AcceptanceResult:
    checks = [
        ("trade count", oos["n_trades"] >= c.min_trades, f"{oos['n_trades']} (need >= {c.min_trades})"),
        ("profit factor", oos["profit_factor"] >= c.min_profit_factor,
         f"{oos['profit_factor']:.2f} (need >= {c.min_profit_factor})"),
        ("max drawdown", oos["max_drawdown_pct"] <= c.max_drawdown_pct,
         f"{oos['max_drawdown_pct']:.1f}% (need <= {c.max_drawdown_pct}%)"),
        ("expectancy (R)", oos["expectancy_r"] >= c.min_expectancy_r,
         f"{oos['expectancy_r']:.3f} (need >= {c.min_expectancy_r})"),
        (f"cost stress x{c.cost_stress_multiplier:g}", stressed["profit_factor"] >= c.min_stressed_profit_factor,
         f"PF {stressed['profit_factor']:.2f} (need >= {c.min_stressed_profit_factor})"),
    ]
    return AcceptanceResult(all(ok for _, ok, _ in checks), checks)


# ---- single-use holdout ------------------------------------------------------
class HoldoutAlreadyUsed(RuntimeError):
    pass


def evaluate_holdout(
    df: pd.DataFrame,
    holdout_start: int,
    strategy_name: str,
    params: dict,
    instrument: Instrument,
    cost: CostModel,
    risk_cfg: RiskConfig,
    equity0: float,
    criteria: Criteria,
    state_dir: str | Path,
    dataset: str,
) -> tuple[AcceptanceResult, dict, dict]:
    """Evaluate ONE final strategy on the locked holdout. This can be done once per dataset.

    This is a guardrail against yourself: a second look at the holdout turns it
    into more training data. (Deleting the marker file defeats it; don't.)
    """
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = state_dir / f"holdout_used_{dataset}.json"
    if marker.exists():
        raise HoldoutAlreadyUsed(
            f"Holdout for {dataset} was already evaluated ({marker}). "
            "Reusing it would contaminate it. Get fresh data (e.g. wait for new bars) instead."
        )
    strat = make_strategy(strategy_name, **params)
    wstart = max(0, holdout_start - strat.warmup_bars)
    sl = df.iloc[wstart:]
    res = run_backtest(sl, strat, instrument, cost, risk_cfg, equity0, trade_from=holdout_start - wstart)
    stressed = run_backtest(sl, strat, instrument, cost.scaled(criteria.cost_stress_multiplier), risk_cfg,
                            equity0, trade_from=holdout_start - wstart)
    verdict = check_acceptance(res.metrics, stressed.metrics, criteria)
    marker.write_text(json.dumps({
        "evaluated_at": datetime.now(timezone.utc).isoformat(), "strategy": strategy_name,
        "params": params, "passed": verdict.passed, "metrics": _clean(res.metrics),
    }, indent=2))
    return verdict, res.metrics, stressed.metrics
