"""cTrader adapter: safety gates + pure decoding logic.

The live wire protocol is verified separately by examples/ctrader_smoke_test.py against real
servers (the build sandbox can't reach cTrader). Here we test what's testable offline: the
fail-closed mode guard, trendbar decoding, and volume conversion.
"""

import types

import pandas as pd
import pytest

from edgelab.brokers.ctrader import (CTraderBroker, CTraderClient, CTraderConfig,
                                     decode_trendbars, PERIOD_MINUTES)
from edgelab.mode import ExecutionMode, ModeError
from edgelab.models import EURUSD


class _FakeClient:
    def __init__(self, host_type, is_live):
        self.cfg = CTraderConfig("cid", "secret", "tok", 49414241, host_type=host_type)
        self._is_live = is_live
    def account_is_live(self): return self._is_live
    def subscribe_spots(self, symbol): pass
    def symbol_id(self, name): return 1
    def reconcile_positions(self): return []


def test_demo_host_pointed_at_live_account_is_refused():
    with pytest.raises(ModeError):
        CTraderBroker(_FakeClient("demo", is_live=True), "EURUSD", EURUSD)


def test_unknown_account_mode_is_refused():
    with pytest.raises(ModeError):
        CTraderBroker(_FakeClient("demo", is_live=None), "EURUSD", EURUSD)


def test_demo_host_with_demo_account_constructs():
    b = CTraderBroker(_FakeClient("demo", is_live=False), "EURUSD", EURUSD)
    assert b.mode is ExecutionMode.DEMO and b.position() is None


def test_config_rejects_bad_host():
    with pytest.raises(ValueError):
        CTraderConfig("c", "s", "t", 1, host_type="paper")


def test_decode_trendbars_matches_ctrader_delta_encoding():
    # low=1.12146 *1e5 = 112146; deltas add on top; smoke test confirmed this on real data
    bar = types.SimpleNamespace(low=112146, deltaOpen=208, deltaHigh=250, deltaClose=40,
                                volume=6922, utcTimestampInMinutes=29839080)
    df = decode_trendbars([bar])
    row = df.iloc[0]
    assert round(row["low"], 5) == 1.12146
    assert round(row["open"], 5) == 1.12354
    assert round(row["high"], 5) == 1.12396
    assert round(row["close"], 5) == 1.12186
    assert row["volume"] == 6922
    assert df.index[0].tzinfo is not None   # UTC-aware


def test_decode_trendbars_empty():
    assert len(decode_trendbars([])) == 0


def test_volume_conversion_snaps_to_step_and_bounds():
    c = CTraderClient(CTraderConfig("c", "s", "t", 1))
    c.symbol_meta[1] = {"minVolume": 100000, "stepVolume": 100000, "maxVolume": 10000000, "digits": 5}
    # 1000 units -> 100000 volume (1 micro lot); exactly min
    assert c.volume_from_units(1, 1000) == 100000
    # 2500 units -> 250000 raw -> snapped down to 200000 (2x step)
    assert c.volume_from_units(1, 2500) == 200000
    # below min -> clamped up to min
    assert c.volume_from_units(1, 100) == 100000


def test_known_timeframes():
    assert PERIOD_MINUTES["H1"] == 60 and "H4" in PERIOD_MINUTES
