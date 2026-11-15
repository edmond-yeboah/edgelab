"""Stop-out detection across legs that SHARE one cTrader client, and deal-history decoding."""

import types

from ctrader_open_api.messages import OpenApiMessages_pb2 as M, OpenApiModelMessages_pb2 as MM

from edgelab.brokers.ctrader import CTraderBroker, CTraderClient, CTraderConfig
from edgelab.models import EURUSD, GBPUSD


def _stop_event(position_id: int, symbol_id: int):
    ev = M.ProtoOAExecutionEvent()
    ev.position.positionId = position_id
    ev.position.positionStatus = 2                      # CLOSED
    ev.position.tradeData.symbolId = symbol_id
    return ev


class _SharedClient(CTraderClient):
    """A real CTraderClient (real event queue) with the network parts faked."""
    def __init__(self):
        super().__init__(CTraderConfig("c", "s", "t", 1, host_type="demo"))
        self.symbols = {"EURUSD": 1, "GBPUSD": 2}
        self.venue = [
            {"positionId": 101, "direction": -1, "volume": 8500000, "entry_price": 1.11, "label": "a", "symbolId": 1},
            {"positionId": 202, "direction": 1, "volume": 8600000, "entry_price": 1.33, "label": "b", "symbolId": 2},
        ]
    def account_is_live(self): return False
    def subscribe_spots(self, s): pass
    def reconcile_positions(self): return list(self.venue)
    def mid_price(self, symbol): return 1.2
    def _refresh_balance(self): pass
    def balance(self): return 100000.0


def _two_legs():
    c = _SharedClient()
    eur, gbp = CTraderBroker(c, "EURUSD", EURUSD), CTraderBroker(c, "GBPUSD", GBPUSD)
    assert eur.position() is not None and gbp.position() is not None      # both adopted
    eur._last_sync = gbp._last_sync = 1e12            # isolate event routing from the venue safety net
    return c, eur, gbp


def test_first_leg_does_not_swallow_another_legs_stopout():
    c, eur, gbp = _two_legs()
    c._exec_events.append(_stop_event(202, 2))        # GBPUSD's stop fires
    assert eur.pop_events() == []                     # EUR leg polls first: not its event, must not eat it
    ev = gbp.pop_events()                             # ...so GBP still sees its own stop-out
    assert [e.reason for e in ev] == ["stop"] and gbp.position() is None
    assert eur.position() is not None                 # and EUR is untouched


def test_each_leg_gets_only_its_own_events():
    c, eur, gbp = _two_legs()
    c._exec_events.extend([_stop_event(101, 1), _stop_event(202, 2)])
    assert [e.reason for e in gbp.pop_events()] == ["stop"]
    assert [e.reason for e in eur.pop_events()] == ["stop"]
    assert c._exec_events == []


def test_safety_net_detects_a_position_that_vanished_without_an_event():
    c, eur, gbp = _two_legs()
    c.venue = [p for p in c.venue if p["positionId"] != 202]        # GBP closed; its event was lost
    gbp._last_sync = 0.0                                            # a sync is due
    assert [e.reason for e in gbp.pop_events()] == ["stop"]
    assert gbp.position() is None
    eur._last_sync = 0.0
    assert eur.pop_events() == [] and eur.position() is not None    # still open on the venue -> kept


def test_safety_net_ignores_a_failed_lookup():
    c, eur, gbp = _two_legs()
    def boom(): raise RuntimeError("network blip")
    c.reconcile_positions = boom
    gbp._last_sync = 0.0
    assert gbp.pop_events() == [] and gbp.position() is not None    # an error must not flip state


def _deal(deal_id, position_id, symbol_id, price, side, closing, **cpd):
    d = MM.ProtoOADeal()
    d.dealId, d.positionId, d.orderId, d.symbolId = deal_id, position_id, deal_id + 1, symbol_id
    d.volume = d.filledVolume = 8500000
    d.executionTimestamp = d.createTimestamp = 1_762_700_000_000 + deal_id
    d.executionPrice, d.tradeSide = price, side
    if closing:
        c = d.closePositionDetail
        c.entryPrice, c.grossProfit, c.swap, c.commission = cpd["entry"], cpd["gross"], cpd["swap"], cpd["comm"]
        c.balance, c.closedVolume, c.moneyDigits = cpd["bal"], 8500000, 2
    return d


def test_deals_are_normalised_with_money_scaled_by_money_digits():
    c = _SharedClient()
    open_deal = _deal(1, 101, 1, 1.11473, 2, closing=False)
    close_deal = _deal(5, 101, 1, 1.12059, 1, closing=True,
                       entry=1.11473, gross=-49810, swap=0, comm=-510, bal=9964417)
    c._call = lambda msg: types.SimpleNamespace(deal=[close_deal, open_deal], hasMore=False)
    deals = c.deals_since(1_762_600_000_000, until_ms=1_762_650_000_000)
    assert [d["deal_id"] for d in deals] == [1, 5]                   # sorted by time
    o, cl = deals
    assert o["is_close"] is False and o["symbol"] == "EURUSD" and o["side"] == "SELL"
    assert cl["is_close"] is True and cl["side"] == "BUY"
    assert cl["gross"] == -498.10 and cl["commission"] == -5.10 and cl["balance"] == 99644.17
    assert cl["entry_price"] == 1.11473 and cl["price"] == 1.12059
