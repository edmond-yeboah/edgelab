"""Execution mode with fail-closed semantics.

Default is DEMO. LIVE needs BOTH an explicit request AND an environment
variable set by a human. Anything unrecognised raises instead of guessing.
"""

from __future__ import annotations

import os
from enum import Enum
from typing import Mapping


class ExecutionMode(str, Enum):
    DEMO = "demo"
    LIVE = "live"


class ModeError(RuntimeError):
    """Raised whenever the execution mode is unclear or inconsistent. Never trade on this."""


LIVE_ENV_VAR = "EDGELAB_ALLOW_LIVE"
LIVE_ENV_VALUE = "I_UNDERSTAND_THE_RISK"


def resolve_mode(requested: str | None, env: Mapping[str, str] | None = None) -> ExecutionMode:
    env = os.environ if env is None else env
    if requested is None:
        return ExecutionMode.DEMO
    try:
        mode = ExecutionMode(str(requested).strip().lower())
    except ValueError:
        raise ModeError(f"Unrecognised execution mode {requested!r}; expected 'demo' or 'live'.") from None
    if mode is ExecutionMode.LIVE and env.get(LIVE_ENV_VAR) != LIVE_ENV_VALUE:
        raise ModeError(
            f"Live trading requested but {LIVE_ENV_VAR} is not set to the required value. "
            "Live mode must be enabled manually, on purpose."
        )
    return mode


def assert_account_matches_mode(mode: ExecutionMode, account_is_live: bool | None) -> None:
    """Call this in every broker adapter before the first order.

    Guards against pointing demo code at a real-money account (or the reverse)
    through mixed-up credentials or endpoints.
    """
    if account_is_live is None:
        raise ModeError("Cannot determine whether the broker account is demo or live; refusing to trade.")
    if mode is ExecutionMode.DEMO and account_is_live:
        raise ModeError("Mode is DEMO but the broker account is LIVE. Refusing to trade.")
    if mode is ExecutionMode.LIVE and not account_is_live:
        raise ModeError("Mode is LIVE but the broker account is DEMO. Refusing to trade.")
