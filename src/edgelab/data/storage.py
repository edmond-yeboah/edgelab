"""Candle storage and data-quality checks. Bad data silently ruins backtests, so check it."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

COLUMNS = ["open", "high", "low", "close"]


class DataError(ValueError):
    pass


def validate_candles(df: pd.DataFrame, max_gap_bars: int = 200) -> list[str]:
    """Return a list of human-readable warnings; raise DataError on unusable data."""
    warnings: list[str] = []
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        raise DataError(f"Missing columns: {missing}")
    if not isinstance(df.index, pd.DatetimeIndex) or df.index.tz is None:
        raise DataError("Index must be a timezone-aware DatetimeIndex (use UTC).")
    if not df.index.is_monotonic_increasing:
        raise DataError("Index is not sorted ascending.")
    if df.index.has_duplicates:
        raise DataError(f"{int(df.index.duplicated().sum())} duplicate timestamps.")
    if df[COLUMNS].isna().any().any():
        raise DataError("NaN values in OHLC.")
    if (df[COLUMNS] <= 0).any().any():
        raise DataError("Non-positive prices in OHLC.")
    bad = (df["high"] < df[["open", "close"]].max(axis=1)) | (df["low"] > df[["open", "close"]].min(axis=1))
    if bad.any():
        raise DataError(f"{int(bad.sum())} bars where high/low do not bracket open/close.")
    if len(df) > 2:
        step = df.index.to_series().diff().median()
        gaps = df.index.to_series().diff() > step * max_gap_bars
        if gaps.any():
            warnings.append(f"{int(gaps.sum())} gaps longer than {max_gap_bars} bars (weekends are normal for FX).")
    return warnings


def save_candles(df: pd.DataFrame, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    validate_candles(df)
    if path.suffix == ".parquet":
        df.to_parquet(path)
    elif path.suffix == ".csv":
        df.to_csv(path)
    else:
        raise ValueError("Use a .parquet or .csv path")


def load_candles(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    if path.suffix == ".parquet":
        df = pd.read_parquet(path)
    elif path.suffix == ".csv":
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
    else:
        raise ValueError("Use a .parquet or .csv path")
    df = df.sort_index()
    validate_candles(df)
    return df
