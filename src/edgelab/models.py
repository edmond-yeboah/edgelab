"""Market-agnostic building blocks: instrument specs and a cost model in basis points.

Costs are expressed in bps of price so the same code handles FX (tiny spreads,
no commission) and crypto spot (small spread, meaningful exchange fee).

Limitation (V1): P&L is computed in the quote currency and assumed equal to the
account currency, which holds for USD-quoted instruments (EURUSD, GBPUSD,
BTCUSD) on a USD account. Add conversion before trading e.g. USDJPY.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Instrument:
    symbol: str
    asset_class: str      # "fx" | "crypto" | ...
    min_qty: float        # smallest tradable size, in base-currency units
    qty_step: float       # size increment

    def round_qty(self, qty: float) -> float:
        """Round DOWN to the broker's size step (never size up)."""
        if not math.isfinite(qty) or qty <= 0:
            return 0.0
        steps = math.floor(qty / self.qty_step + 1e-9)
        return round(steps * self.qty_step, 10)


# Typical retail specs. CHECK YOUR BROKER'S ACTUAL CONTRACT SPECS before relying on these.
EURUSD = Instrument("EURUSD", "fx", min_qty=1_000, qty_step=1_000)        # 1 micro lot
GBPUSD = Instrument("GBPUSD", "fx", min_qty=1_000, qty_step=1_000)
BTCUSD = Instrument("BTCUSD", "crypto", min_qty=0.0001, qty_step=0.0001)


@dataclass(frozen=True)
class CostModel:
    spread_bps: float = 0.0              # full quoted spread; half is paid on each side
    fee_bps_per_side: float = 0.0        # commission / exchange fee on notional, each side
    slippage_bps_per_side: float = 0.0   # extra adverse move on each fill

    @property
    def adverse_fraction(self) -> float:
        """Price concession per fill, as a fraction of price (half spread + slippage)."""
        return (self.spread_bps / 2.0 + self.slippage_bps_per_side) / 1e4

    @property
    def fee_fraction(self) -> float:
        return self.fee_bps_per_side / 1e4

    def scaled(self, k: float) -> "CostModel":
        """Return a copy with every cost multiplied by k (used for cost-stress tests)."""
        return CostModel(self.spread_bps * k, self.fee_bps_per_side * k, self.slippage_bps_per_side * k)
