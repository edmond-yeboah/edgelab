from __future__ import annotations

from pathlib import Path

import yaml

from .models import CostModel
from .risk import RiskConfig
from .validation import Criteria

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "default.yaml"


def load_config(path: str | Path | None = None, asset_class: str = "fx") -> tuple[RiskConfig, CostModel, Criteria]:
    raw = yaml.safe_load(Path(path or DEFAULT_PATH).read_text())
    try:
        costs = raw["costs"][asset_class]
    except KeyError:
        raise KeyError(f"No cost model for asset class {asset_class!r} in config") from None
    return RiskConfig.from_dict(raw["risk"]), CostModel(**costs), Criteria.from_dict(raw["acceptance"])
