from __future__ import annotations

import math

import numpy as np
import pandas as pd

EMPTY = {
    "n_trades": 0, "net_pnl": 0.0, "return_pct": 0.0, "win_rate": 0.0, "avg_win": 0.0,
    "avg_loss": 0.0, "expectancy": 0.0, "expectancy_r": 0.0, "profit_factor": 0.0,
    "max_drawdown_pct": 0.0, "total_fees": 0.0,
}


def max_drawdown_pct(curve: np.ndarray) -> float:
    if len(curve) == 0:
        return 0.0
    peak = np.maximum.accumulate(curve)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peak > 0, (peak - curve) / peak, 0.0)
    return float(dd.max() * 100)


def compute(trades: pd.DataFrame, start_equity: float, equity: pd.Series | None = None) -> dict:
    """Performance summary, all AFTER costs.

    If a bar-by-bar equity series is given, drawdown uses it (includes open-trade
    swings); otherwise drawdown is measured on closed trades only.
    """
    if trades is None or len(trades) == 0:
        return dict(EMPTY)
    pnl = trades["pnl"].to_numpy(dtype=float)
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    gross_win, gross_loss = float(wins.sum()), float(-losses.sum())
    if gross_loss > 0:
        pf = gross_win / gross_loss
    else:
        pf = math.inf if gross_win > 0 else 0.0
    curve = equity.to_numpy(dtype=float) if equity is not None and len(equity) else \
        np.concatenate([[start_equity], start_equity + np.cumsum(pnl)])
    return {
        "n_trades": int(len(pnl)),
        "net_pnl": float(pnl.sum()),
        "return_pct": float(pnl.sum() / start_equity * 100),
        "win_rate": float(len(wins) / len(pnl)),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "expectancy": float(pnl.mean()),
        "expectancy_r": float(np.nanmean(trades["r_multiple"].to_numpy(dtype=float))),
        "profit_factor": float(pf),
        "max_drawdown_pct": max_drawdown_pct(curve),
        "total_fees": float(trades["fees"].sum()),
    }
