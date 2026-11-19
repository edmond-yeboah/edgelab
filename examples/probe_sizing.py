"""READ-ONLY: show, for a hypothetical account size, which markets the bot could actually trade.

Answers "what would the bot do with $X?" WITHOUT opening a new account or placing anything. It runs
the bot's real sizing path (risk.size) against the live venue minimums and the current H4 stop widths,
at an equity you pass in. If 0.5% of the account buys less than the broker's minimum trade, the engine
rejects it ("below_min_size") and the bot simply does not trade that market.

Safe to run while the live bot is running: read_only session (never refreshes the token), no orders.

    cd ~/edgelab && source .venv/bin/activate
    set -a; source .env; set +a
    python examples/probe_sizing.py --equity 800
    python examples/probe_sizing.py --equity 800 --risk-pct 2.0   # what a higher risk % would unlock
"""
from __future__ import annotations

import argparse
import os
import time

from edgelab.brokers.ctrader import CTraderClient, CTraderConfig
from edgelab.models import Instrument
from edgelab.risk import RiskConfig, RiskEngine
from edgelab.strategies import make_strategy

SYMBOLS = ["EURUSD", "GBPUSD", "XAUUSD", "BTCUSD"]
ASSET = {"BTCUSD": "crypto"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--equity", type=float, required=True, help="hypothetical account size to test")
    ap.add_argument("--risk-pct", type=float, default=0.5, help="risk per trade %% (strategy uses 0.5)")
    a = ap.parse_args()

    cfg = CTraderConfig(
        os.environ["CTRADER_CLIENT_ID"], os.environ["CTRADER_CLIENT_SECRET"],
        os.environ["CTRADER_ACCESS_TOKEN"], int(os.environ["CTRADER_ACCOUNT_ID"]),
        host_type="demo",
        refresh_token=os.environ.get("CTRADER_REFRESH_TOKEN", ""),
        token_store=os.environ.get("CTRADER_TOKEN_STORE", ".ctrader_refresh_token"),
        read_only=True,   # never refresh the token; never knock the live bot offline
    )
    client = CTraderClient(cfg)
    client.connect()
    for s in SYMBOLS:
        client.subscribe_spots(s)
    time.sleep(3)   # let a few spot ticks arrive

    strat = make_strategy("ema_trend", fast=30, slow=150, atr_mult=2.0)
    risk_cfg = RiskConfig(risk_per_trade_pct=a.risk_pct, daily_loss_limit_pct=100.0,
                          max_drawdown_halt_pct=100.0, max_leverage=500.0)
    budget = a.equity * a.risk_pct / 100.0

    print(f"\nHypothetical account = ${a.equity:,.2f}   risk/trade = {a.risk_pct}%  (= ${budget:,.2f} per trade)")
    print("=" * 92)
    tradeable = []
    for sym in SYMBOLS:
        sid = client.symbol_id(sym)
        meta = client._ensure_symbol_meta(sid)
        mid = client.mid_price(sym)
        asset = ASSET.get(sym, "fx")
        instrument = Instrument(sym, asset, min_qty=max(meta["minVolume"] / 100.0, 1e-9),
                                qty_step=max(meta["stepVolume"] / 100.0, 1e-9))
        risk = RiskEngine(risk_cfg, instrument)

        hist = client.get_trendbars(sym, "H4", 400)
        stop_distance = float(strat.signals(hist)["stop_distance"].iloc[-1])
        if not mid or stop_distance <= 0:
            print(f"\n{sym:7} — no mid price / stop yet; skip")
            continue

        # what the strategy WANTS to risk vs the smallest trade the broker allows
        min_units = instrument.min_qty
        min_trade_risk = min_units * stop_distance        # $ risked by one minimum-size trade
        decision = risk.size(a.equity, mid, stop_distance, open_positions=0)

        verdict = "TRADES" if decision.approved else f"NO TRADE ({decision.reason})"
        pct_at_min = 100.0 * min_trade_risk / a.equity
        print(f"\n{sym:7} stop={stop_distance:<10.5g} min trade = {min_units:g} units -> risks "
              f"${min_trade_risk:,.2f} ({pct_at_min:.2f}% of account)")
        print(f"        budget ${budget:,.2f}  ->  {verdict}"
              + (f"  qty={decision.qty:g}" if decision.approved else ""))
        if decision.approved:
            tradeable.append(sym)

    print("\n" + "=" * 92)
    if tradeable:
        print(f"At ${a.equity:,.0f} and {a.risk_pct}% risk, the bot could trade: {', '.join(tradeable)}")
    else:
        print(f"At ${a.equity:,.0f} and {a.risk_pct}% risk, the bot could trade NOTHING — every market's "
              f"smallest allowed trade risks more than ${budget:,.2f}. A demo at this size would sit idle.")
    print("(Read-only probe — nothing was placed, token untouched.)")


if __name__ == "__main__":
    main()
