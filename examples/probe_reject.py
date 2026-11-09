"""Probe WHY XAUUSD / BTCUSD orders get rejected while EURUSD / GBPUSD fill.

Run it with the bot STOPPED (so there's only one connection to cTrader):

    sudo systemctl stop edgelab
    set -a; source .env; set +a
    python examples/probe_reject.py
    sudo systemctl start edgelab

For each of gold and BTC it prints the symbol's volume spec and the FULL raw response to a
market order (executionType, errorCode, description) — that names the exact reason. It closes
any position it accidentally opens, so it leaves the account as it found it.
"""
from __future__ import annotations

import os
import time

from edgelab.brokers.ctrader import CTraderClient, CTraderConfig


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

    # representative stop distances in price units (gold ~ $20, BTC ~ $2000)
    for sym, qty, stop in [("XAUUSD", 6.0, 20.0), ("BTCUSD", 0.2, 2000.0)]:
        print("=" * 60)
        print(f"{sym}  qty={qty}  stop={stop}")
        try:
            sid = client.symbol_id(sym)
            meta = client._ensure_symbol_meta(sid)
            print(f"  symbolId={sid}  meta={meta}")
            print(f"  volume_from_units({qty}) -> {client.volume_from_units(sid, qty)}")
            print(f"  mid_price={client.mid_price(sym)}  market_open={client.is_market_open(sym)}")
        except Exception as e:
            print("  meta/lookup error:", repr(e))
            continue
        try:
            res = client.new_market_order(sym, -1, qty, stop, f"probe-{sym}")
            raw = res["raw"]
            print("  response class:", type(raw).__name__)
            print("  executionType:", getattr(raw, "executionType", None))
            print("  errorCode:", getattr(raw, "errorCode", None))
            print("  FULL >>>", " ".join(str(raw).split())[:1000])
        except Exception as e:
            print("  order raised:", repr(e))
        time.sleep(2)
        # leave the account clean: close anything that actually opened
        try:
            for p in client.reconcile_positions():
                if p["symbolId"] == sid:
                    print("  !! a position opened — closing it:", p["positionId"])
                    client.close_position(p["positionId"], p["volume"])
        except Exception as e:
            print("  cleanup error:", repr(e))

    print("=" * 60)
    print("done")


if __name__ == "__main__":
    main()
