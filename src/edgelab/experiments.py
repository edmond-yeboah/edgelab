"""Append-only log of every strategy variant you evaluate.

Why: if you try 200 parameter sets, the best one looks good by luck alone.
Knowing the trial count keeps you honest about how much to trust a "winner".
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path


def _clean(x):
    if isinstance(x, float) and not math.isfinite(x):
        return None if math.isnan(x) else ("inf" if x > 0 else "-inf")
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    return x


class ExperimentLog:
    TRIAL_STAGES = ("train", "research")

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, stage: str, strategy: str, params: dict, dataset: str, metrics: dict, **extra) -> None:
        row = {
            "ts": datetime.now(timezone.utc).isoformat(), "stage": stage, "strategy": strategy,
            "params": params, "dataset": dataset, "metrics": metrics, **extra,
        }
        with self.path.open("a") as f:
            f.write(json.dumps(_clean(row), default=str) + "\n")

    def rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def n_trials(self, dataset: str | None = None) -> int:
        return sum(
            1 for r in self.rows()
            if r["stage"] in self.TRIAL_STAGES and (dataset is None or r["dataset"] == dataset)
        )
