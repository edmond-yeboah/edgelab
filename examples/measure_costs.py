"""Measure REAL spreads on your cTrader demo, so backtests use true costs instead of placeholders.

Run on your Mac (needs cTrader access + your env vars), ideally during active market hours
(London/New York) — spreads widen at rollover and in thin hours.

    python examples/measure_costs.py --seconds 120

For each symbol it watches the live bid/ask and reports the spread in basis points (bps of price).
It prints suggested `spread_bps` values for config/default.yaml.

IMPORTANT — this measures SPREAD only. Your account also pays COMMISSION (FP Markets Raw cTrader),
which is a separate fixed fee per lot. Get that number from FP Markets' contract specs and set it as
`fee_bps_per_side`:  fee_bps = commission_per_side_in_quote / notional * 1e4.
e.g. $3 per side per 100k lot -> 3/100000*1e4 = 0.3 bps/side.
"""

from __future__ import annotations

import argparse
import os
import statistics
import time

from edgelab.brokers.ctrader import CTraderClient, CTraderConfig

DEFAULT_SYMBOLS = ["EURUSD", "GBPUSD", "XAUUSD", "BTCUSD"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    ap.add_argument("--seconds", type=int, default=120, help="sampling window per run")
    ap.add_argument("--interval", type=float, default=0.5, help="seconds between samples")
    a = ap.parse_args()

    req = ["CTRADER_CLIENT_ID", "CTRADER_CLIENT_SECRET", "CTRADER_ACCESS_TOKEN", "CTRADER_ACCOUNT_ID"]
    missing = [k for k in req if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"Missing env vars: {missing}. See SETUP_CTRADER.md.")
    cfg = CTraderConfig(os.environ["CTRADER_CLIENT_ID"], os.environ["CTRADER_CLIENT_SECRET"],
                        os.environ["CTRADER_ACCESS_TOKEN"], int(os.environ["CTRADER_ACCOUNT_ID"]),
                        host_type="demo")
    client = CTraderClient(cfg)
    client.connect()

    found = []
    for s in a.symbols:
        try:
            client.subscribe_spots(s)
            found.append(s)
        except KeyError:
            print(f"  (skipping {s}: not available on this account)")
    print(f"\nSampling spreads for {found} over {a.seconds}s — keep it running during active hours...\n")

    samples: dict[str, list[float]] = {s: [] for s in found}
    t_end = time.time() + a.seconds
    while time.time() < t_end:
        for s in found:
            d = client._spot.get(client.symbol_id(s))
            if d and "bid" in d and "ask" in d and d["ask"] > d["bid"] > 0:
                mid = (d["bid"] + d["ask"]) / 2
                samples[s].append((d["ask"] - d["bid"]) / mid * 1e4)  # bps
        time.sleep(a.interval)

    print(f"{'symbol':10} {'samples':>7} {'median_bps':>11} {'mean_bps':>9} {'min':>6} {'max':>6}")
    print("-" * 55)
    suggested = {}
    for s in found:
        xs = samples[s]
        if not xs:
            print(f"{s:10} {'0':>7}  (no ticks — market closed or illiquid; try active hours)")
            continue
        med = statistics.median(xs)
        suggested[s] = round(med, 2)
        print(f"{s:10} {len(xs):7d} {med:11.2f} {statistics.mean(xs):9.2f} {min(xs):6.2f} {max(xs):6.2f}")

    print("\nSuggested config/default.yaml spread_bps (median; round up a touch for safety):")
    for s, v in suggested.items():
        print(f"  {s}: spread_bps ≈ {v}")
    print("\nThen add commission as fee_bps_per_side (see header). Re-run the basket with real costs after.")


if __name__ == "__main__":
    main()
