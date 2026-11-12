"""Probe WHY XAUUSD / BTCUSD orders don't fill while EURUSD / GBPUSD do.

Run with the bot STOPPED (single clean connection), and with NO manual test positions open:

    sudo systemctl stop edgelab
    set -a; source .env; set +a
    python examples/probe_reject.py
    sudo systemctl start edgelab

For gold and BTC it sends a market order TWO ways — once with NO stop-loss, once WITH the
server-side stop the bot attaches — and reports whether each one actually filled. If the
no-stop one fills and the with-stop one doesn't, the attached stop is the culprit. It closes
anything it opens, so the account is left as it was.
"""
from __future__ import annotations

import os
import time

from edgelab.brokers.ctrader import CTraderClient, CTraderConfig


def _cleanup(client, sid) -> bool:
    filled = False
    for p in client.reconcile_positions():
        if p["symbolId"] == sid:
            filled = True
            print(f"    -> FILLED: position {p['positionId']} vol {p['volume']} — closing")
            try:
                client.close_position(p["positionId"], p["volume"])
            except Exception as e:
                print("       close error:", repr(e))
            time.sleep(1)
    return filled


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

    # Stops are deliberately the RAW, non-round ATR values production sends (not 20.0 / 2000.0):
    # round numbers happened to be valid tick multiples and hid the INVALID_REQUEST bug.
    for sym, qty, stop in [("XAUUSD", 6.0, 64.51975218141366), ("BTCUSD", 0.2, 2009.4990976184747)]:
        sid = client.symbol_id(sym)
        print("=" * 60)
        try:
            meta = client._ensure_symbol_meta(sid)
            print(f"{sym}  symbolId={sid}  meta={meta}  volume={client.volume_from_units(sid, qty)}")
        except Exception as e:
            print(f"{sym}  meta error: {e!r}")
            continue
        for variant, stp in [("NO-STOP", 0.0), ("WITH-STOP", stop)]:
            print(f"  --- {sym} {variant} (stop_distance={stp}) ---")
            try:
                raw = client.new_market_order(sym, -1, qty, stp, f"probe-{sym}")["raw"]
                order = getattr(raw, "order", None)
                print("    executionType:", getattr(raw, "executionType", None),
                      "| executedVolume:", getattr(order, "executedVolume", None),
                      "| errorCode:", repr(getattr(raw, "errorCode", "")))
            except Exception as e:
                print("    order raised:", repr(e))
            time.sleep(2)
            _cleanup(client, sid)

    print("=" * 60)
    print("done")


if __name__ == "__main__":
    main()
