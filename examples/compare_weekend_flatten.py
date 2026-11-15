"""Compare HOLD-through-weekends (what the live bot does) vs FLATTEN-on-Friday / re-enter-on-Monday.

Same strategy, same costs, same data; the only difference is that in the FLATTEN run an open
position is closed at the close of the last bar before a market-closure gap and the strategy
re-enters after it if its signal still says so. Markets that never close (BTC) have no gaps, so
they are identical in both runs.

    # on the server (has the cTrader credentials). Read-only: it only downloads candles.
    cd ~/edgelab && source .venv/bin/activate && set -a; source .env; set +a
    python examples/compare_weekend_flatten.py --fetch --start-year 2015

    # later runs reuse the saved candles (data/<SYMBOL>_h4.parquet):
    python examples/compare_weekend_flatten.py

If you still have the long research candles (2010-2026) as parquet/csv, put them in data/ as
<SYMBOL>_h4.parquet and skip --fetch: a longer history makes the comparison far more trustworthy.
"""
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd

from edgelab.backtest import run_backtest
from edgelab.config import load_config
from edgelab.data.storage import load_candles, save_candles
from edgelab.metrics import compute, max_drawdown_pct
from edgelab.models import BTCUSD, EURUSD, GBPUSD, Instrument
from edgelab.risk import RiskConfig
from edgelab.strategies import make_strategy

SYMBOLS = ["EURUSD", "GBPUSD", "XAUUSD", "BTCUSD"]
INSTRUMENTS = {"EURUSD": EURUSD, "GBPUSD": GBPUSD, "BTCUSD": BTCUSD,
               "XAUUSD": Instrument("XAUUSD", "fx", min_qty=0.01, qty_step=0.01)}
ASSET = {"BTCUSD": "crypto"}      # same mapping as the live bot (gold uses the fx cost model)


# ---- data ---------------------------------------------------------------------------------
def fetch_h4(client, sym: str, start_year: int) -> pd.DataFrame:
    """Walk backwards from now in 120-day windows until history runs out. Read-only."""
    from edgelab.brokers.ctrader import decode_trendbars
    sid = client.symbol_id(sym)
    period = client._s["Mo"].ProtoOATrendbarPeriod.Value("H4")
    floor_ms = int(pd.Timestamp(f"{start_year}-01-01", tz="UTC").timestamp() * 1000)
    to_ms, step = int(time.time() * 1000), 120 * 86400 * 1000
    frames, empty, errors = [], 0, 0
    while to_ms > floor_ms and empty < 3 and errors < 5:
        frm = max(floor_ms, to_ms - step)
        df = pd.DataFrame()
        try:
            res = client._call(client._req("ProtoOAGetTrendbarsReq", ctidTraderAccountId=client.cfg.account_id,
                                           symbolId=sid, period=period, fromTimestamp=frm, toTimestamp=to_ms))
            df = decode_trendbars(res.trendbar)
        except Exception as e:                      # keep going; a bad window shouldn't kill the run
            errors += 1
            print(f"   {sym}: window error ({errors}): {e!r}")
        if len(df):
            frames.append(df)
            empty = 0
        else:
            empty += 1
        to_ms = frm - 1
        time.sleep(0.25)                            # stay well under the API rate limit
    if not frames:
        raise SystemExit(f"{sym}: no candles returned")
    out = pd.concat(frames).sort_index()
    return out[~out.index.duplicated(keep="first")]


def get_data(args) -> dict[str, pd.DataFrame]:
    ddir = Path(args.data_dir)
    data = {}
    if args.fetch:
        from edgelab.brokers.ctrader import CTraderClient, CTraderConfig
        cfg = CTraderConfig(
            os.environ["CTRADER_CLIENT_ID"], os.environ["CTRADER_CLIENT_SECRET"],
            os.environ["CTRADER_ACCESS_TOKEN"], int(os.environ["CTRADER_ACCOUNT_ID"]), host_type="demo",
            refresh_token=os.environ.get("CTRADER_REFRESH_TOKEN", ""),
            token_store=os.environ.get("CTRADER_TOKEN_STORE", ".ctrader_refresh_token"))
        client = CTraderClient(cfg)
        client.connect()
        for sym in args.symbols:
            print(f"fetching {sym} H4 ...")
            df = fetch_h4(client, sym, args.start_year)
            save_candles(df[["open", "high", "low", "close"]], ddir / f"{sym}_h4.parquet")
            print(f"   {sym}: {len(df)} bars  {df.index[0]:%Y-%m-%d} -> {df.index[-1]:%Y-%m-%d}")
    for sym in args.symbols:
        p = ddir / f"{sym}_h4.parquet"
        if not p.exists():
            p = ddir / f"{sym}_h4.csv"
        if not p.exists():
            raise SystemExit(f"missing {ddir}/{sym}_h4.parquet — run with --fetch, or add the file")
        data[sym] = load_candles(p)
    return data


# ---- backtest -----------------------------------------------------------------------------
def run_symbol(sym: str, df: pd.DataFrame, risk_pct: float, equity: float, flatten: bool):
    _, cost, _ = load_config(asset_class=ASSET.get(sym, "fx"))
    risk = RiskConfig(risk_per_trade_pct=risk_pct, daily_loss_limit_pct=100.0,
                      max_drawdown_halt_pct=100.0, max_leverage=500.0)
    strat = make_strategy("ema_trend", fast=30, slow=150, atr_mult=2.0)
    res = run_backtest(df, strat, INSTRUMENTS[sym], cost, risk, equity, reentry_cooldown_bars=1,
                       flatten_gap_hours=24 if flatten else None)
    return res.trades


def summarize(trades: pd.DataFrame, equity0: float) -> dict:
    if len(trades) == 0:
        return {"trades": 0}
    t = trades.sort_values("exit_time")
    m = compute(t, equity0)                       # closed-trade drawdown on pooled/stand-alone stream
    r = t["r_multiple"].to_numpy(dtype=float)
    gap = t[t["exit_reason"] == "stop_gap"]
    return {
        "trades": m["n_trades"], "PF": m["profit_factor"], "exp_R": m["expectancy_r"],
        "net_%": m["return_pct"], "maxDD_%": m["max_drawdown_pct"], "worst_R": float(np.nanmin(r)),
        "gap_stops": len(gap), "gap_avg_R": float(gap["r_multiple"].mean()) if len(gap) else float("nan"),
        "wkend_flat": int((t["exit_reason"] == "weekend_flat").sum()),
    }


def monthly_pnl(trades: pd.DataFrame) -> pd.Series:
    t = trades.copy()
    t["m"] = pd.to_datetime(t["exit_time"]).dt.tz_convert("UTC").dt.tz_localize(None).dt.to_period("M")
    return t.groupby("m")["pnl"].sum()


def paired_month_ci(hold: pd.Series, flat: pd.Series, n: int = 5000, seed: int = 1):
    """FLATTEN minus HOLD, month by month (same calendar months, so market moves cancel out).
    Returns (mean diff, 5th, 95th percentile of the bootstrapped mean, months)."""
    months = hold.index.union(flat.index)
    d = (flat.reindex(months, fill_value=0.0) - hold.reindex(months, fill_value=0.0)).to_numpy(float)
    rng = np.random.default_rng(seed)
    means = rng.choice(d, size=(n, len(d)), replace=True).mean(axis=1)
    return float(d.mean()), float(np.percentile(means, 5)), float(np.percentile(means, 95)), len(d)


def table(rows: list[tuple[str, dict, dict]]) -> None:
    cols = ["trades", "PF", "exp_R", "net_%", "maxDD_%", "worst_R", "gap_stops", "gap_avg_R", "wkend_flat"]
    print(f"{'':<14}{'':<8}" + "".join(f"{c:>11}" for c in cols))
    for name, hold, flat in rows:
        for label, d in (("HOLD", hold), ("FLATTEN", flat)):
            vals = "".join(f"{d.get(c, float('nan')):>11.2f}" if isinstance(d.get(c), float) else
                           f"{d.get(c, 0):>11}" for c in cols)
            print(f"{name if label == 'HOLD' else '':<14}{label:<8}{vals}")
        print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true", help="download H4 candles from cTrader first (read-only)")
    ap.add_argument("--start-year", type=int, default=2015)
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--symbols", nargs="+", default=SYMBOLS)
    ap.add_argument("--risk-pct", type=float, default=0.25, help="0.25 matches the published basket result")
    ap.add_argument("--equity", type=float, default=1_000_000.0, help="large so lot minimums never bind")
    a = ap.parse_args()

    data = get_data(a)
    print("\nDATA")
    for sym, df in data.items():
        gaps = (df.index.to_series().diff() > pd.Timedelta(hours=24)).sum()
        print(f"  {sym}: {len(df)} bars  {df.index[0]:%Y-%m-%d} -> {df.index[-1]:%Y-%m-%d}  "
              f"closure gaps >24h: {gaps}")

    rows, pool_h, pool_f = [], [], []
    for sym, df in data.items():
        h, f = run_symbol(sym, df, a.risk_pct, a.equity, False), run_symbol(sym, df, a.risk_pct, a.equity, True)
        pool_h.append(h)
        pool_f.append(f)
        rows.append((sym, summarize(h, a.equity), summarize(f, a.equity)))
    ph, pf = pd.concat(pool_h), pd.concat(pool_f)
    n = len(data)
    rows.append(("POOLED", summarize(ph, a.equity * n), summarize(pf, a.equity * n)))

    print(f"\nRESULTS  (risk {a.risk_pct}%/trade, EMA 30/150, ATR x2, H4; R = multiples of planned risk)")
    table(rows)

    base = a.equity * n
    mean, lo, hi, months = paired_month_ci(monthly_pnl(ph), monthly_pnl(pf))
    print("NOTE: FLATTEN splits one trend into many short trades, so per-trade PF / exp_R are NOT comparable")
    print("between the two runs. Judge them on net_%, maxDD_% and worst_R.\n")
    print("NOISE CHECK — FLATTEN minus HOLD, paired by calendar month (pooled, all markets):")
    print(f"  mean difference  {mean / base * 100:+.3f}% of equity per month   "
          f"90% interval [{lo / base * 100:+.3f}%, {hi / base * 100:+.3f}%]   over {months} months")
    verdict = ("FLATTEN looks better" if lo > 0 else "HOLD looks better" if hi < 0
               else "indistinguishable from noise")
    print(f"  -> {verdict}. Only change the live bot if the interval excludes zero AND maxDD/worst_R "
          f"improve too.")

if __name__ == "__main__":
    main()
