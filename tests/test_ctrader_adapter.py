"""cTrader adapter: safety gates + pure decoding logic.

The live wire protocol is verified separately by examples/ctrader_smoke_test.py against real
servers (the build sandbox can't reach cTrader). Here we test what's testable offline: the
fail-closed mode guard, trendbar decoding, and volume conversion.
"""

import time
import types

import pandas as pd
import pytest

from edgelab.brokers.ctrader import (CTraderBroker, CTraderClient, CTraderConfig, CTraderError,
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


def _client_with_fake_transport(monkeypatch, extracted):
    """A CTraderClient whose _call returns `extracted` from the wire (no real network)."""
    import twisted.internet.threads as tthreads
    c = CTraderClient(CTraderConfig("c", "s", "t", 1))
    c._reactor = object()
    c._client = types.SimpleNamespace(send=lambda *a, **k: None)
    monkeypatch.setattr(tthreads, "blockingCallFromThread", lambda reactor, fn, *a: "raw-resp")
    c._s = dict(c._s)
    c._s["Protobuf"] = types.SimpleNamespace(extract=lambda resp: extracted)
    return c


def test_call_raises_ctrader_error_on_error_response(monkeypatch):
    # A ProtoOAErrorRes must become a clean CTraderError, never an AttributeError crash.
    class ProtoOAErrorRes:
        errorCode = "ACCOUNT_NOT_AUTHORIZED"
        description = "token invalid"
    c = _client_with_fake_transport(monkeypatch, ProtoOAErrorRes())
    with pytest.raises(CTraderError) as ei:
        c._call(object())
    assert "ACCOUNT_NOT_AUTHORIZED" in str(ei.value)


def test_call_returns_extracted_on_success(monkeypatch):
    class ProtoOAAccountAuthRes:   # any non-error type passes through
        pass
    ok = ProtoOAAccountAuthRes()
    c = _client_with_fake_transport(monkeypatch, ok)
    assert c._call(object()) is ok


def test_try_refresh_token_returns_false_on_error_instead_of_raising(monkeypatch):
    # A failed refresh must not raise (so auth can fall back to the existing access token).
    class ProtoOAErrorRes:
        errorCode = "CH_ACCESS_TOKEN_INVALID"
        description = ""
    c = _client_with_fake_transport(monkeypatch, ProtoOAErrorRes())
    c.cfg.refresh_token = "whatever"
    assert c._try_refresh_token() is False


def test_is_market_open_tracks_tick_freshness():
    c = CTraderClient(CTraderConfig("c", "s", "t", 1))
    c.symbols["EURUSD"] = 1
    assert c.is_market_open("EURUSD") is False            # no tick ever -> closed
    c._spot[1] = {"bid": 1.1, "ask": 1.1, "ts": time.time()}
    assert c.is_market_open("EURUSD") is True             # fresh tick -> open
    c._spot[1]["ts"] = time.time() - 10_000               # stale (weekend)
    assert c.is_market_open("EURUSD") is False


class _FillClient:
    """Fake client: records orders; reconcile shows a filled position iff `fills` is True."""
    def __init__(self, fills=True):
        self.cfg = CTraderConfig("c", "s", "t", 49414241, host_type="demo")
        self.fills = fills
        self.orders = []
        self._positions = []
    def account_is_live(self): return False
    def subscribe_spots(self, s): pass
    def symbol_id(self, name): return 1
    def reconcile_positions(self): return list(self._positions)
    def is_market_open(self, symbol, max_age=300): return True
    def mid_price(self, symbol): return 1.10
    def balance(self): return 100000.0
    def volume_from_units(self, sid, qty): return int(qty * 100)
    def new_market_order(self, symbol, direction, units, stop, label):
        self.orders.append(label)
        if self.fills:
            self._positions.append({"positionId": 555, "direction": direction,
                                    "volume": int(units * 100), "entry_price": 110000,
                                    "label": label, "symbolId": 1})
        return {"raw": None}


def test_submit_market_records_position_only_on_confirmed_fill():
    c = _FillClient(fills=True)
    b = CTraderBroker(c, "EURUSD", EURUSD)
    pos = b.submit_market(1, 1000.0, 0.001, 50.0)
    assert pos is not None and b.position() is not None
    assert getattr(b.position(), "venue_id", None) == 555
    assert round(b.position().entry_price, 5) == 1.10000     # from the venue fill, not a guess


def test_submit_market_stays_flat_when_fill_not_confirmed(monkeypatch):
    from edgelab.brokers import ctrader as ct
    monkeypatch.setattr(ct.time, "sleep", lambda s: None)    # don't actually wait through retries
    c = _FillClient(fills=False)
    b = CTraderBroker(c, "EURUSD", EURUSD)
    assert b.submit_market(1, 1000.0, 0.001, 50.0) is None   # no venue position -> no phantom
    assert b.position() is None
    assert c.orders                                          # the order WAS sent


def test_broker_market_open_delegates_to_client():
    assert CTraderBroker(_FillClient(), "EURUSD", EURUSD).market_open() is True
