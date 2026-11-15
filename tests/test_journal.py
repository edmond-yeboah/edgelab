from datetime import datetime, timezone

from edgelab.live.journal import MIN_TRADES_FOR_COMPARISON, TradeJournal

T0 = 1_762_700_000.0          # an arbitrary "now" (epoch seconds)


class _Clock:
    def __init__(self, t=T0): self.t = t
    def __call__(self): return self.t


class _FakeClient:
    """Returns whatever deals were 'executed' at or after the requested time."""
    def __init__(self): self.deals, self.calls, self.fail = [], 0, False
    def deals_since(self, since_ms):
        self.calls += 1
        if self.fail:
            raise RuntimeError("api down")
        return [d for d in self.deals if d["ts_ms"] >= since_ms]


def _close_deal(deal_id, position_id, ts_s, *, symbol="EURUSD", side="BUY", price=1.12059, entry=1.11473,
                gross=-498.10, swap=0.0, comm=-5.10):
    return {"deal_id": deal_id, "position_id": position_id, "order_id": deal_id, "symbol_id": 1,
            "symbol": symbol, "ts_ms": int(ts_s * 1000), "price": price, "side": side, "volume": 8500000,
            "is_close": True, "entry_price": entry, "gross": gross, "swap": swap, "commission": comm,
            "balance": 99644.17, "closed_volume": 8500000}


def _journal(tmp_path, clock=None):
    return TradeJournal(tmp_path / "j", clock=clock or _Clock())


def _entry(j, pid=101, direction=-1, fill=1.11473, ref=1.11470, stop_distance=0.00586, risk=500.0):
    return j.record_entry(symbol="EURUSD", position_id=pid, direction=direction, qty=85000.0,
                          fill_price=fill, ref_price=ref, stop_distance=stop_distance,
                          planned_stop=fill - direction * stop_distance, risk_amount=risk, label="x")


def test_entry_slippage_is_positive_when_filled_worse_than_decision_price(tmp_path):
    j = _journal(tmp_path)
    short_worse = j.record_entry(symbol="E", position_id=1, direction=-1, fill_price=1.1000, ref_price=1.1001)
    long_worse = j.record_entry(symbol="E", position_id=2, direction=1, fill_price=1.1001, ref_price=1.1000)
    assert short_worse["slippage_bps"] > 0 and long_worse["slippage_bps"] > 0     # both paid up
    assert j.record_entry(symbol="E", position_id=3, direction=1, fill_price=1.1, ref_price=None)["slippage_bps"] is None


def test_first_sync_only_starts_the_clock(tmp_path):
    j, c = _journal(tmp_path), _FakeClient()
    c.deals = [_close_deal(1, 101, T0 - 86400)]               # history from before the journal existed
    assert j.sync(c) == [] and j.records("close") == []
    assert c.calls == 0                                       # nothing fetched on the first call


def test_close_is_journaled_from_deal_history_and_joined_to_its_entry(tmp_path):
    clock = _Clock()
    j, c = _journal(tmp_path, clock), _FakeClient()
    j.sync(c)
    _entry(j)
    c.deals = [_close_deal(5, 101, T0 + 600)]
    (rec,) = j.sync(c)
    assert rec["net"] == -503.20 and rec["gross"] == -498.10 and rec["commission"] == -5.10
    assert rec["bot"] is True and rec["direction"] == -1 and rec["symbol"] == "EURUSD"
    assert rec["r"] == round(-503.20 / 500.0, 3)              # -1.006R
    # short entered 1.11473, planned stop 1.12059; exited exactly there -> a stop, no overshoot
    assert rec["exit_kind"] == "stop" and abs(rec["stop_overshoot_r"]) < 0.01


def test_stop_overshoot_shows_a_gap_fill_and_signal_exits_are_not_stops(tmp_path):
    j, c = _journal(tmp_path), _FakeClient()
    j.sync(c)
    _entry(j, pid=101)                                         # short, stop at 1.12059
    _entry(j, pid=102)
    c.deals = [_close_deal(5, 101, T0 + 60, price=1.12359),    # gapped 30 pips THROUGH the stop
               _close_deal(6, 102, T0 + 120, price=1.10500, gross=800.0)]   # exited well in profit
    gap, sig = j.sync(c)
    assert gap["exit_kind"] == "stop" and gap["stop_overshoot_r"] > 0.4
    assert sig["exit_kind"] == "signal" and sig["stop_overshoot_r"] < 0


def test_resync_does_not_duplicate_and_unknown_positions_are_flagged_not_bot(tmp_path):
    j, c = _journal(tmp_path), _FakeClient()
    j.sync(c)
    c.deals = [_close_deal(5, 999, T0 + 60, symbol="XAUUSD")]  # e.g. a manual order, never entered by the bot
    assert len(j.sync(c)) == 1
    assert j.sync(c) == [] and j.sync(c) == []                 # the overlap re-reads it; dedupe holds
    assert j.records("close")[0]["bot"] is False


def test_state_survives_restart_and_startup_sync_announces_only_missed_closes(tmp_path):
    clock = _Clock()
    j1, c = _journal(tmp_path, clock), _FakeClient()
    j1.sync(c)
    c.deals = [_close_deal(5, 101, T0 + 60)]
    j1.sync(c)                                                  # closed while running: journaled, not announced
    c.deals.append(_close_deal(6, 102, T0 + 300, symbol="GBPUSD"))    # closed while the bot was down

    events = []
    j2 = _journal(tmp_path, clock)                              # "restart": a fresh object, same files
    assert j2.startup_sync(c, lambda k, d: events.append((k, d))) == 1
    assert [(k, d["symbol"]) for k, d in events] == [("missed_close", "GBPUSD")]
    assert len(j2.records("close")) == 2


def test_startup_sync_survives_an_api_failure(tmp_path):
    j, c = _journal(tmp_path), _FakeClient()
    j.sync(c)
    c.fail = True
    assert j.startup_sync(c, lambda k, d: None) == 0


def test_weekly_due_is_sunday_evening_once_per_week(tmp_path):
    j = _journal(tmp_path)
    sun_eve = datetime(2026, 11, 15, 18, 5, tzinfo=timezone.utc)      # Sunday
    assert j.weekly_due(sun_eve)
    assert not j.weekly_due(datetime(2026, 11, 15, 17, 59, tzinfo=timezone.utc))   # too early
    assert not j.weekly_due(datetime(2026, 11, 16, 18, 5, tzinfo=timezone.utc))    # Monday
    j.mark_weekly_sent(sun_eve)
    assert not j.weekly_due(datetime(2026, 11, 15, 23, 0, tzinfo=timezone.utc))    # already sent
    assert _journal(tmp_path).weekly_due(sun_eve) is False                        # ...even after a restart
    assert j.weekly_due(datetime(2026, 11, 22, 18, 5, tzinfo=timezone.utc))        # next week


def test_weekly_summary_is_honest_about_small_samples(tmp_path):
    j, c = _journal(tmp_path), _FakeClient()
    j.sync(c)
    _entry(j, pid=101)
    _entry(j, pid=102)
    c.deals = [_close_deal(5, 101, T0 + 60), _close_deal(6, 102, T0 + 120, gross=800.0, comm=-5.0, price=1.10)]
    j.sync(c)
    now = datetime.fromtimestamp(T0 + 3600, timezone.utc)
    text = j.weekly_summary(now, 99_000.0, {"EURUSD": {"dir": -1, "qty": 85000.0}, "GBPUSD": None})
    assert "2 closed (1W/1L)" in text
    assert "net +291.80" in text or "net +291.8" in text            # -503.20 + 795.00
    assert "Open now: EURUSD SHORT" in text and "$99,000.00" in text
    assert f"far too few to judge (need ~{MIN_TRADES_FOR_COMPARISON}+)" in text
    assert "Backtest expects" in text


def test_weekly_summary_drops_the_small_sample_warning_with_enough_trades(tmp_path):
    j, c = _journal(tmp_path), _FakeClient()
    j.sync(c)
    for i in range(MIN_TRADES_FOR_COMPARISON):
        _entry(j, pid=1000 + i)
        c.deals.append(_close_deal(100 + i, 1000 + i, T0 + 60 + i, gross=100.0, comm=0.0, price=1.10))
    j.sync(c)
    text = j.weekly_summary(datetime.fromtimestamp(T0 + 7200, timezone.utc), None, None)
    assert "far too few" not in text and "PF" in text


def test_tick_syncs_on_a_schedule_and_sends_the_weekly_once(tmp_path):
    clock, sent = _Clock(), []
    j, c = _journal(tmp_path, clock), _FakeClient()
    sun = datetime(2026, 11, 15, 19, 0, tzinfo=timezone.utc)
    args = dict(client=c, on_event=lambda k, d: sent.append((k, d)), equity_fn=lambda: 1.0,
                positions_fn=lambda: {})
    j.tick(now=sun, **args)
    assert [k for k, _ in sent] == ["weekly"]
    j.tick(now=sun, **args)
    assert [k for k, _ in sent] == ["weekly"]                   # not twice
    j.sync(c)                                                    # clock started
    n = c.calls
    j.tick(now=sun, **args)
    assert c.calls == n                                          # 0s later: throttled, no API call
    clock.t += 130                                               # past SYNC_EVERY
    j.tick(now=sun, **args); j.tick(now=sun, **args)
    assert c.calls == n + 1                                      # exactly one sync for the two ticks


def test_tick_never_raises_even_if_everything_fails(tmp_path):
    j, c = _journal(tmp_path), _FakeClient()
    j.sync(c)
    c.fail = True
    def boom(): raise RuntimeError("no equity")
    sun = datetime(2026, 11, 15, 19, 0, tzinfo=timezone.utc)
    j.tick(client=c, on_event=lambda k, d: None, equity_fn=boom, positions_fn=lambda: {}, now=sun)


def test_a_torn_last_line_does_not_break_reading(tmp_path):
    j = _journal(tmp_path)
    _entry(j)
    with open(j.path, "a") as f:
        f.write('{"type": "close", "deal_id": 7, "net"')        # crash mid-write
    assert len(j.records()) == 1


def test_seeded_entries_make_adopted_positions_count_as_bot_trades(tmp_path):
    j = TradeJournal(str(tmp_path))
    pos = [{"symbol": "XAUUSD", "position_id": 7, "direction": -1, "qty": 7.0, "entry_price": 2000.0}]
    assert j.seed_open_positions(pos, 500.0) == 1
    assert j.seed_open_positions(pos, 500.0) == 0            # idempotent
    e = j.records("entry")[0]
    assert e["seeded"] is True and e["risk_amount"] == 500.0
    d = {"deal_id": 1, "position_id": 7, "symbol": "XAUUSD", "ts_ms": 1, "price": 1990.0, "side": "BUY",
         "volume": 700, "closed_volume": 700, "entry_price": 2000.0, "gross": 250.0, "swap": 0.0,
         "commission": 0.0, "balance": 1}
    rec = j._close_record(d, {x["position_id"]: x for x in j.records("entry")}[7])
    assert rec["bot"] is True and rec["r"] == 0.5
