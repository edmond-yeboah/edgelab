from __future__ import annotations

from .base import Strategy
from .ema_trend import EmaTrend
from .mean_reversion import BollingerReversion
from .indicators import RsiReversion, MacdTrend, DonchianBreakout

STRATEGIES: dict[str, type[Strategy]] = {
    EmaTrend.name: EmaTrend,
    BollingerReversion.name: BollingerReversion,
    RsiReversion.name: RsiReversion,
    MacdTrend.name: MacdTrend,
    DonchianBreakout.name: DonchianBreakout,
}


def make_strategy(name: str, **params) -> Strategy:
    try:
        return STRATEGIES[name](**params)
    except KeyError:
        raise KeyError(f"Unknown strategy {name!r}; registered: {sorted(STRATEGIES)}") from None


__all__ = ["Strategy", "EmaTrend", "BollingerReversion", "RsiReversion", "MacdTrend",
           "DonchianBreakout", "STRATEGIES", "make_strategy"]
