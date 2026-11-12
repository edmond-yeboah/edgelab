"""READ-ONLY: print the exact order parameters the bot would send for each symbol, WITHOUT
placing anything. Pinpoints why XAUUSD/BTCUSD get INVALID_REQUEST while EURUSD/GBPUSD are fine.

Safe to run while the bot is live — it only reads prices and trendbars.

    cd ~/edgelab && source .venv/bin/activate
    set -a; source .env; set +a
    python examples/probe_values.py
"""
from __future__ import annotations

import os
import time

from edgelab.brokers.ctrader import CTraderClient, CTraderConfig, SCALE
from edgelab.models import Instrument
from edgelab.risk import RiskConfig, RiskEngine
from edgelab.strategies import make_strategy

SYMBOLS = ["EURUSD", "GBPUSD", "XAUUSD", "BTCUSD"]
ASSET = {"BTCUSD": "crypto"}


def main() -> None:
    cfg = CTraderConfig(
        os.environ["CTRADER_CLIENT_ID"], os.environ["CTRADER_CLIENT_SECRET"],
        os.environ["CTRADER_ACCESS_TOKEN"], int(os.environ["CTRADER_ACCOUNT_ID"]),
        host_type="demo",
        refresh_token=os.environ.get("CTRADER_REFRESH_TOKEN", ""),
        token_store=os.environ.get("CTRADER_TOKEN_STORE", ".ctrader_refresh_token"),
    )
    client = CTraderClient(cfg)
    client.connect()
    for s in SYMBOLS:
        client.subscribe_spots(s)
    client._refresh_balance()
    equity = client.balance() or 0.0
    time.sleep(3)   # let a few spot ticks arrive

    strat = make_strategy("ema_trend", fast=30, slow=150, atr_mult=2.0)
    risk_cfg = RiskConfig(risk_per_trade_pct=0.5, daily_loss_limit_pct=100.0,
                          max_drawdown_halt_pct=100.0, max_leverage=500.0)

    print(f"\nbalance/equity = {equity:.2f}\n" + "=" * 78)
    for sym in SYMBOLS:
        sid = client.symbol_id(sym)
        meta = client._ensure_symbol_meta(sid)
        mid = client.mid_price(sym)
        asset = ASSET.get(sym, "fx")
        instrument = Instrument(sym, asset, min_qty=max(meta["minVolume"] / 100.0, 1e-9),
                                qty_step=max(meta["stepVolume"] / 100.0, 1e-9))
        risk = RiskEngine(risk_cfg, instrument)

        # same signal the bot acts on
        hist = client.get_trendbars(sym, "H4", 400)
        sig = strat.signals(hist)
        direction = int(sig["direction"].iloc[-1])
        stop_distance = float(sig["stop_distance"].iloc[-1])

        print(f"\n{sym}  sid={sid}  digits={meta['digits']}  "
              f"minVol={meta['minVolume']} stepVol={meta['stepVolume']} maxVol={meta['maxVolume']}")
        print(f"  last close (decoded) = {float(hist['close'].iloc[-1]):.5f}   mid_price = {mid}")
        print(f"  signal direction     = {direction}")
        print(f"  stop_distance (price)= {stop_distance}")
        if mid and stop_distance > 0:
            decision = risk.size(equity, mid, stop_distance, open_positions=0)
            print(f"  risk.size -> approved={decision.approved} qty={getattr(decision, 'qty', None)} "
                  f"reason={getattr(decision, 'reason', '')}")
            if decision.approved:
                vol = client.volume_from_units(sid, decision.qty)
                rel_sl = int(round(stop_distance * SCALE))
                print(f"  --> VOLUME sent        = {vol}   (min={meta['minVolume']} max={meta['maxVolume']})")
                print(f"  --> relativeStopLoss   = {rel_sl}   (= stop_distance x {int(SCALE)})")
        else:
            print("  (no mid price yet or non-positive stop; can't size)")
    print("\n" + "=" * 78 + "\ndone — nothing was placed")


if __name__ == "__main__":
    main()
