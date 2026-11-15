"""READ-ONLY: print the broker's raw deal history so the journal's net-P&L formula can be verified.

The journal computes   net = grossProfit + swap + closePositionDetail.commission.
cTrader's app showed the 2026-11-09 EURUSD stop-out as  gross -498.10, fees -5.10, net -503.20.
This prints every deal's raw money fields (and both ways of adding up the commission) so we can
see which one reproduces that known number before trusting the weekly summary.

    cd ~/edgelab && source .venv/bin/activate && set -a; source .env; set +a
    python examples/probe_deals.py            # last 8 days
    python examples/probe_deals.py --days 14

Places no orders. Safe to run while the bot is running.
"""
from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timezone

from edgelab.brokers.ctrader import CTraderClient, CTraderConfig


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=8)
    a = ap.parse_args()
    cfg = CTraderConfig(
        os.environ["CTRADER_CLIENT_ID"], os.environ["CTRADER_CLIENT_SECRET"],
        os.environ["CTRADER_ACCESS_TOKEN"], int(os.environ["CTRADER_ACCOUNT_ID"]), host_type="demo",
        refresh_token=os.environ.get("CTRADER_REFRESH_TOKEN", ""),
        token_store=os.environ.get("CTRADER_TOKEN_STORE", ".ctrader_refresh_token"),
        read_only=True)  # never refresh the token: that would knock the running bot offline
    client = CTraderClient(cfg)
    client.connect()

    until = int(time.time() * 1000)
    res = client._call(client._req("ProtoOADealListReq", ctidTraderAccountId=cfg.account_id,
                                   fromTimestamp=until - a.days * 86400 * 1000, toTimestamp=until, maxRows=500))
    deals = sorted(res.deal, key=lambda d: d.executionTimestamp)
    names = {i: n for n, i in client.symbols.items()}
    print(f"{len(deals)} deals in the last {a.days} days (hasMore={res.hasMore})\n")
    for d in deals:
        when = datetime.fromtimestamp(d.executionTimestamp / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        side = "BUY " if d.tradeSide == 1 else "SELL"
        print(f"{when}  deal {d.dealId}  pos {d.positionId}  {names.get(d.symbolId, d.symbolId):<7} "
              f"{side} vol={d.filledVolume or d.volume}  @ {d.executionPrice}  "
              f"deal.commission={d.commission}  deal.moneyDigits={d.moneyDigits}")
        if d.HasField("closePositionDetail"):
            c = d.closePositionDetail
            k = 10 ** int(c.moneyDigits or d.moneyDigits or 2)
            gross, swap, ccomm, dcomm = c.grossProfit / k, c.swap / k, c.commission / k, d.commission / k
            print(f"      CLOSE  entry={c.entryPrice}  gross={gross:.2f}  swap={swap:.2f}  "
                  f"closeDetail.commission={ccomm:.2f}  deal.commission={dcomm:.2f}  balance={c.balance / k:.2f}")
            print(f"      net if commission = closeDetail.commission:        {gross + swap + ccomm:.2f}   <- journal uses this")
            print(f"      net if commission = closeDetail + deal.commission: {gross + swap + ccomm + dcomm:.2f}")
    print("\nCheck: the 2026-11-09 EURUSD SELL 85k stop-out should show net -503.20 "
          "(gross -498.10, fees -5.10). Paste this output back so the formula can be confirmed.")


if __name__ == "__main__":
    main()
