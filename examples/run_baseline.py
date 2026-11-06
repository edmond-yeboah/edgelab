"""End-to-end research workflow on the baseline EMA strategy.

    python examples/run_baseline.py --synthetic random      # no edge exists: expect REJECT
    python examples/run_baseline.py --synthetic trending    # edge exists:    expect better results
    python examples/run_baseline.py --data data/eurusd_h1.parquet --symbol EURUSD

Steps: split off a locked holdout -> walk-forward on the dev data (every trial logged)
-> cost-stress -> acceptance gates -> (optionally) ONE look at the holdout.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from edgelab.config import load_config
from edgelab.data.storage import load_candles
from edgelab.data.synthetic import random_walk, trending
from edgelab.experiments import ExperimentLog
from edgelab.models import BTCUSD, EURUSD, GBPUSD
from edgelab.validation import (HoldoutAlreadyUsed, check_acceptance, chrono_split, dataset_id,
                                evaluate_holdout, walk_forward)

INSTRUMENTS = {"EURUSD": EURUSD, "GBPUSD": GBPUSD, "BTCUSD": BTCUSD}
GRID = {"fast": [10, 20, 30], "slow": [60, 100, 150], "atr_mult": [2.0, 3.0]}


def fmt(m: dict) -> str:
    return (f"trades={m['n_trades']}  PF={m['profit_factor']:.2f}  expectancy={m['expectancy_r']:.3f}R  "
            f"net=${m['net_pnl']:.2f}  maxDD={m['max_drawdown_pct']:.1f}%  win={m['win_rate']:.0%}")


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--data", help="candles .parquet/.csv (UTC index, open/high/low/close)")
    g.add_argument("--synthetic", choices=["random", "trending"], default="random")
    ap.add_argument("--symbol", default="EURUSD", choices=sorted(INSTRUMENTS))
    ap.add_argument("--timeframe", default="1h")
    ap.add_argument("--equity", type=float, default=500.0)
    ap.add_argument("--train-bars", type=int, default=6000)
    ap.add_argument("--test-bars", type=int, default=3000)
    ap.add_argument("--evaluate-holdout", action="store_true",
                    help="spend the ONE-TIME holdout look (only when you have a final candidate)")
    ap.add_argument("--state-dir", default="experiments")
    a = ap.parse_args()

    instrument = INSTRUMENTS[a.symbol]
    risk_cfg, cost, crit = load_config(asset_class=instrument.asset_class)
    if a.data:
        df, label = load_candles(a.data), Path(a.data).stem
    else:
        df = (random_walk if a.synthetic == "random" else trending)(n=40_000, seed=7)
        label = f"synthetic_{a.synthetic}"
        print(f"** SYNTHETIC {a.synthetic} data: pipeline sanity check only, says nothing about real markets **\n")

    dev, holdout = chrono_split(df, holdout_frac=0.2)
    ds = dataset_id(a.symbol, a.timeframe, df) + f"_{label}"
    log = ExperimentLog(Path(a.state_dir) / "trials.jsonl")
    print(f"dataset {ds}: {len(dev)} dev bars, {len(holdout)} locked holdout bars")

    wf = walk_forward(dev, "ema_trend", GRID, instrument, cost, risk_cfg, a.equity,
                      a.train_bars, a.test_bars, log=log, dataset=ds)
    stressed = walk_forward(dev, "ema_trend", GRID, instrument, cost.scaled(crit.cost_stress_multiplier),
                            risk_cfg, a.equity, a.train_bars, a.test_bars)
    print(f"\nwalk-forward windows: {len(wf.windows)} ({sum(w['params'] is not None for w in wf.windows)} tradable)")
    print("out-of-sample :", fmt(wf.metrics))
    print(f"cost x{crit.cost_stress_multiplier:g}      :", fmt(stressed.metrics))
    verdict = check_acceptance(wf.metrics, stressed.metrics, crit)
    print("\nacceptance on dev walk-forward:", "PASS" if verdict.passed else "REJECT")
    print(verdict.report())
    print(f"\ntrials logged for this dataset so far: {log.n_trials(ds)} (the more you try, the less a 'winner' means)")

    if a.evaluate_holdout:
        # Pick ONE final candidate. Here: the most frequently selected walk-forward parameters.
        chosen = [w["params"] for w in wf.windows if w["params"]]
        if not chosen:
            print("\nNo tradable parameters found; nothing to take to the holdout.")
            return
        best = max(chosen, key=chosen.count)
        try:
            hv, hm, hs = evaluate_holdout(df, len(dev), "ema_trend", best, instrument, cost, risk_cfg,
                                          a.equity, crit, a.state_dir, ds)
            print(f"\nHOLDOUT (one-time) params={best}\n  ", fmt(hm), "\n   stressed:", fmt(hs))
            print("holdout verdict:", "PASS" if hv.passed else "REJECT")
            print(hv.report())
        except HoldoutAlreadyUsed as e:
            print("\n", e)


if __name__ == "__main__":
    main()
