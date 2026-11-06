"""Screen every registered strategy on one dataset, honestly, in one shot.

Walk-forward (out-of-sample) + a 2x-cost stress for each strategy, ranked. This is EXPLORATORY —
it does not touch any holdout. Anything that looks promising here must then be confirmed on a
SEPARATE, untouched dataset (a different pair or timeframe) before it means anything.

    python examples/screen_strategies.py --data data/eurusd_h1.parquet --symbol EURUSD
"""

from __future__ import annotations

import argparse

from edgelab.config import load_config
from edgelab.data.storage import load_candles
from edgelab.models import BTCUSD, EURUSD, GBPUSD
from edgelab.validation import chrono_split, walk_forward, check_acceptance

INSTRUMENTS = {"EURUSD": EURUSD, "GBPUSD": GBPUSD, "BTCUSD": BTCUSD}

# Small grids on purpose: fewer trials = less chance of a lucky winner.
GRIDS = {
    "ema_trend": {"fast": [10, 20, 50], "slow": [100, 200], "atr_mult": [2.0, 3.0]},
    "bollinger_reversion": {"lookback": [20, 50], "n_std": [2.0, 2.5], "atr_mult": [2.0, 3.0]},
    "rsi_reversion": {"n": [7, 14], "oversold": [25, 30], "overbought": [70, 75], "atr_mult": [2.0, 3.0]},
    "macd_trend": {"fast": [12], "slow": [26], "signal": [9], "atr_mult": [2.0, 3.0]},
    "donchian_breakout": {"n": [20, 55], "atr_mult": [2.0, 3.0]},
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--symbol", default="EURUSD", choices=sorted(INSTRUMENTS))
    ap.add_argument("--equity", type=float, default=500.0)
    ap.add_argument("--train-bars", type=int, default=6000)
    ap.add_argument("--test-bars", type=int, default=3000)
    a = ap.parse_args()

    instrument = INSTRUMENTS[a.symbol]
    risk_cfg, cost, crit = load_config(asset_class=instrument.asset_class)
    df = load_candles(a.data)
    dev, _ = chrono_split(df, holdout_frac=0.2)   # never screen on the holdout
    print(f"{a.symbol}: screening {len(dev)} dev bars (holdout withheld)\n")

    rows = []
    for name, grid in GRIDS.items():
        wf = walk_forward(dev, name, grid, instrument, cost, risk_cfg, a.equity, a.train_bars, a.test_bars)
        st = walk_forward(dev, name, grid, instrument, cost.scaled(crit.cost_stress_multiplier),
                          risk_cfg, a.equity, a.train_bars, a.test_bars)
        m = wf.metrics
        passed = check_acceptance(m, st.metrics, crit).passed
        rows.append((name, m["n_trades"], m["profit_factor"], m["expectancy_r"], m["net_pnl"],
                     m["max_drawdown_pct"], st.metrics["profit_factor"], passed))

    rows.sort(key=lambda r: r[3], reverse=True)  # by expectancy
    print(f"{'strategy':22} {'trades':>6} {'PF':>6} {'exp(R)':>8} {'net$':>9} {'maxDD%':>7} {'PFx2':>6}  verdict")
    print("-" * 82)
    for name, n, pf, ex, net, dd, pf2, passed in rows:
        print(f"{name:22} {n:6d} {pf:6.2f} {ex:8.3f} {net:9.1f} {dd:7.1f} {pf2:6.2f}  "
              f"{'PASS' if passed else 'reject'}")
    print("\nExploratory only. Confirm any PASS on a separate untouched dataset before trusting it.")


if __name__ == "__main__":
    main()
