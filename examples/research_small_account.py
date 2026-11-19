"""OPTION 4 research: is there an FX-only basket that is (a) tradeable on a small (~$800) account and
(b) still has a validated edge, now that gold and BTC are too big to size?

It fetches the cheap-minimum FX majors (all 0.01-lot / 1000-unit minimums, so all viable on $800),
backtests the SAME validated rule with NO re-tuning (ema_trend 30/150, ATR x2, H4), checks which
pairs can actually size at the given account/risk, and then replays every pair's trades onto ONE
shared account to get the real basket equity curve, drawdown, losing streak and kill-switch behaviour.

Read-only (never refreshes the token): safe to run while the live bot is running.

    cd ~/edgelab && source .venv/bin/activate && set -a; source .env; set +a
    python examples/research_small_account.py --fetch --start-year 2015 --equity 800 --risk-pct 0.75
    python examples/research_small_account.py --equity 800 --risk-pct 0.75    # reuse saved candles

Candles are saved on the SERVER in data/<SYMBOL>_h4.parquet and reused on later runs.
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
from edgelab.metrics import max_drawdown_pct
from edgelab.models import Instrument
from edgelab.risk import RiskConfig
from edgelab.strategies import make_strategy

# Clean USD-quoted majors only (5-digit, pip 0.0001, min 1000 units). USD/JPY and USD-base pairs
# are left out of the first pass to avoid pip-scaling noise; add them once this shape is proven.
DEFAULT_SYMBOLS = ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"]
# Baskets to compare: name -> the pairs in it.
BASKETS = {
    "EUR+GBP (2)":           ["EURUSD", "GBPUSD"],
    "EUR+GBP+AUD+NZD (4)":   ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"],
}


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
        except Exception as e:
            errors += 1
            print(f"   {sym}: window error ({errors}): {e!r}")
        if len(df):
            frames.append(df); empty = 0
        else:
            empty += 1
        to_ms = frm - 1
        time.sleep(0.25)
    if not frames:
        raise SystemExit(f"{sym}: no candles returned")
    out = pd.concat(frames).sort_index()
    return out[~out.index.duplicated(keep="first")]


def get_data(args) -> dict[str, pd.DataFrame]:
    ddir = Path(args.data_dir)
    if args.fetch:
        from edgelab.brokers.ctrader import CTraderClient, CTraderConfig
        cfg = CTraderConfig(
            os.environ["CTRADER_CLIENT_ID"], os.environ["CTRADER_CLIENT_SECRET"],
            os.environ["CTRADER_ACCESS_TOKEN"], int(os.environ["CTRADER_ACCOUNT_ID"]), host_type="demo",
            refresh_token=os.environ.get("CTRADER_REFRESH_TOKEN", ""),
            token_store=os.environ.get("CTRADER_TOKEN_STORE", ".ctrader_refresh_token"),
            read_only=True)   # never refresh the token: would knock the running bot offline
        client = CTraderClient(cfg)
        client.connect()
        for sym in args.symbols:
            print(f"fetching {sym} H4 ...")
            df = fetch_h4(client, sym, args.start_year)
            save_candles(df[["open", "high", "low", "close"]], ddir / f"{sym}_h4.parquet")
            print(f"   {sym}: {len(df)} bars  {df.index[0]:%Y-%m-%d} -> {df.index[-1]:%Y-%m-%d}")
    data = {}
    for sym in args.symbols:
        p = ddir / f"{sym}_h4.parquet"
        if not p.exists():
            raise SystemExit(f"missing {ddir}/{sym}_h4.parquet — run once with --fetch")
        data[sym] = load_candles(p)
    return data


def backtest_pair(sym: str, df: pd.DataFrame, risk_pct: float, equity: float) -> pd.DataFrame:
    _, cost, _ = load_config(asset_class="fx")
    risk = RiskConfig(risk_per_trade_pct=risk_pct, daily_loss_limit_pct=100.0,
                      max_drawdown_halt_pct=100.0, max_leverage=500.0)   # kill-switch applied later, at basket level
    strat = make_strategy("ema_trend", fast=30, slow=150, atr_mult=2.0)
    inst = Instrument(sym, "fx", min_qty=1_000, qty_step=1_000)
    res = run_backtest(df, strat, inst, cost, risk, equity, reentry_cooldown_bars=1)
    return res.trades


def pooled_account(trades_by_sym: dict[str, pd.DataFrame], syms: list[str], equity0: float,
                   risk_frac: float, kill_dd: float = 0.20) -> dict:
    """Replay every pair's trades onto ONE account, chronologically by exit time.
    pnl = risk_frac * equity_at_trade * r_multiple (r already includes costs). Compounding, with a
    20% peak-to-trough kill-switch. Approximation: ignores concurrent open-position overlap."""
    rows = []
    for s in syms:
        t = trades_by_sym[s]
        for _, r in t.iterrows():
            rows.append((r["exit_time"], float(r["r_multiple"]), r["exit_reason"]))
    rows.sort(key=lambda x: x[0])
    if not rows:
        return {"trades": 0}

    equity, peak, maxdd = equity0, equity0, 0.0
    curve = [equity0]
    wins = losses = 0
    gross_win = gross_loss = 0.0
    streak = worst_streak = 0
    killed = None
    for ts, r, reason in rows:
        pnl = risk_frac * equity * r
        equity += pnl
        curve.append(equity)
        peak = max(peak, equity)
        maxdd = max(maxdd, (peak - equity) / peak)
        if r >= 0:
            wins += 1; gross_win += pnl; streak = 0
        else:
            losses += 1; gross_loss += -pnl
            streak += 1; worst_streak = max(worst_streak, streak)
        if killed is None and (peak - equity) / peak >= kill_dd:
            killed = ts
    n = wins + losses
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    exp_r = float(np.mean([r for _, r, _ in rows]))
    return {
        "trades": n, "win_rate": 100.0 * wins / n if n else 0.0,
        "pf": pf, "exp_r": exp_r,
        "net_pct": 100.0 * (equity - equity0) / equity0,
        "end_equity": equity, "maxdd_pct": 100.0 * maxdd,
        "worst_losing_streak": worst_streak,
        "killed_at": killed,
        "span": f"{rows[0][0]:%Y-%m-%d} -> {rows[-1][0]:%Y-%m-%d}",
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--start-year", type=int, default=2015)
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--equity", type=float, default=800.0)
    ap.add_argument("--risk-pct", type=float, default=0.75)
    a = ap.parse_args()

    data = get_data(a)
    risk_frac = a.risk_pct / 100.0
    budget = a.equity * risk_frac

    print(f"\n{'='*92}\nSMALL-ACCOUNT RESEARCH  equity=${a.equity:,.0f}  risk/trade={a.risk_pct}% (${budget:,.2f})"
          f"  rule: ema_trend 30/150 ATR x2 H4 (no re-tuning)\n{'='*92}")

    # Per-pair: stand-alone stats + can it size at this account/risk?
    trades_by_sym = {}
    print(f"\n{'pair':8}{'bars':>7}{'trades':>8}{'PF':>7}{'exp_R':>8}{'maxDD%':>9}{'min-trade risk':>16}{'sizes?':>9}")
    for sym in a.symbols:
        df = data[sym]
        t = backtest_pair(sym, df, a.risk_pct, a.equity)
        trades_by_sym[sym] = t
        # min-trade risk at current price: 1000 units * recent stop distance
        strat = make_strategy("ema_trend", fast=30, slow=150, atr_mult=2.0)
        stop = float(strat.signals(df)["stop_distance"].dropna().iloc[-1])
        min_risk = 1000 * stop
        sizes = "YES" if min_risk <= budget else "no"
        if len(t):
            r = t["r_multiple"].to_numpy(float)
            pf = r[r > 0].sum() / -r[r < 0].sum() if (r < 0).any() else float("inf")
            eq = np.concatenate([[a.equity], a.equity + np.cumsum(risk_frac * a.equity * r)])
            dd = max_drawdown_pct(eq)
            print(f"{sym:8}{len(df):>7}{len(t):>8}{pf:>7.2f}{r.mean():>8.2f}{dd:>9.2f}"
                  f"{'$'+format(min_risk,'.2f'):>16}{sizes:>9}")
        else:
            print(f"{sym:8}{len(df):>7}{0:>8}{'-':>7}{'-':>8}{'-':>9}{'$'+format(min_risk,'.2f'):>16}{sizes:>9}")

    # Baskets: shared $800 account, chronological, 20% kill-switch
    print(f"\n{'='*92}\nBASKET (one shared ${a.equity:,.0f} account, 20% kill-switch)\n{'-'*92}")
    print(f"{'composition':22}{'trades':>8}{'win%':>7}{'PF':>7}{'exp_R':>8}{'net%':>9}{'maxDD%':>9}"
          f"{'lose-streak':>13}{'killed?':>18}")
    for name, syms in BASKETS.items():
        syms = [s for s in syms if s in trades_by_sym]
        if not syms:
            continue
        r = pooled_account(trades_by_sym, syms, a.equity, risk_frac)
        if r["trades"] == 0:
            continue
        killed = "-" if r["killed_at"] is None else f"{r['killed_at']:%Y-%m-%d}"
        pf = "inf" if r["pf"] == float("inf") else f"{r['pf']:.2f}"
        print(f"{name:22}{r['trades']:>8}{r['win_rate']:>7.1f}{pf:>7}{r['exp_r']:>8.2f}"
              f"{r['net_pct']:>9.1f}{r['maxdd_pct']:>9.2f}{r['worst_losing_streak']:>13}{killed:>18}")

    print(f"\n{'-'*92}")
    print("Read as: a basket is worth pursuing only if PF > ~1.3, maxDD stays well under 20% (no kill),")
    print("and the losing streak is survivable. 'killed' = a 20% drawdown that would have flattened the")
    print("account. Caveat: pooled by exit time, equal risk, ignores concurrent-position overlap; H4 only.")
    print("(Read-only research — nothing placed, token untouched.)")


if __name__ == "__main__":
    main()
