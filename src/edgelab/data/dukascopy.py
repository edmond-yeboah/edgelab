"""Free historical candles from Dukascopy (via the `dukascopy-python` package).

    pip install "edgelab[data]"
    python -m edgelab.data.dukascopy EUR_USD 2021-01-01 2026-01-01 --out data/eurusd_h1.parquet

Notes
  * Dukascopy BID prices are NOT your broker's prices. Treat this as research
    data; measure your broker's real spreads on its demo account.
  * dukascopy-python 5.0.0 fails to import on Python <= 3.13 (it uses
    `requests`/`DataFeed` in annotations without importing them). `_import_duka`
    works around it; on Python 3.14+ the workaround is not needed.
  * Written against the library's inspected API; not exercised against the live
    Dukascopy servers from the build sandbox. Run it once yourself and eyeball
    the output (the validator in storage.py will catch structural problems).
"""

from __future__ import annotations

import argparse
import builtins
from datetime import datetime, timezone

import pandas as pd

from .storage import save_candles, validate_candles


def _import_duka():
    try:
        import dukascopy_python as duka
        return duka
    except NameError:
        import requests  # type: ignore
        builtins.requests = requests          # type: ignore[attr-defined]
        builtins.DataFeed = object            # type: ignore[attr-defined]
        try:
            import dukascopy_python as duka
        finally:
            del builtins.requests              # type: ignore[attr-defined]
            del builtins.DataFeed              # type: ignore[attr-defined]
        return duka
    except ImportError as e:
        raise ImportError("Install the data extra: pip install 'edgelab[data]'") from e


def fetch(instrument_code: str, start: datetime, end: datetime, hours: int = 1) -> pd.DataFrame:
    """Fetch BID candles of `hours`-hour bars, month by month. `instrument_code` like 'EUR_USD'."""
    duka = _import_duka()
    from dukascopy_python import instruments

    instrument = getattr(instruments, f"I_{instrument_code}")
    frames = []
    cursor = start
    while cursor < end:
        nxt = min(end, (cursor.replace(day=1) + pd.offsets.MonthBegin(1)).to_pydatetime())
        candles = duka.fetch_candles(
            instrument=instrument, interval=hours, time_unit=duka.TimeUnit.HOUR,
            offer_side=duka.OfferSide.BID, start=cursor, end=nxt, version=1,
        )
        part = duka.to_dataframe(list(candles))
        if len(part):
            frames.append(part)
        cursor = nxt
    if not frames:
        raise RuntimeError("No data returned")
    df = pd.concat(frames)
    df = df[~df.index.duplicated(keep="first")].sort_index()
    keep = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    return df[keep]


def main() -> None:
    ap = argparse.ArgumentParser(description="Download Dukascopy candles to a parquet/csv file")
    ap.add_argument("instrument", help="e.g. EUR_USD, GBP_USD, BTC_USD")
    ap.add_argument("start", help="YYYY-MM-DD (UTC)")
    ap.add_argument("end", help="YYYY-MM-DD (UTC)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--hours", type=int, default=1, help="bar size in hours")
    a = ap.parse_args()
    parse = lambda s: datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    df = fetch(a.instrument, parse(a.start), parse(a.end), a.hours)
    for w in validate_candles(df):
        print("warning:", w)
    save_candles(df, a.out)
    print(f"saved {len(df)} bars {df.index[0]} -> {df.index[-1]} to {a.out}")


if __name__ == "__main__":
    main()
