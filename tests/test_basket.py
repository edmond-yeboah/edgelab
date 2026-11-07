"""BasketRunner: drives several legs off one clock, with an account-level kill switch."""

import pathlib
import sys
import types

import pandas as pd

from edgelab.brokers.paper import PaperBroker
from edgelab.config import load_config
from edgelab.data.synthetic import trending
from edgelab.live.basket import BasketConfig, BasketRunner, ReplayClock
from edgelab.live.runner import DemoRunner, RunnerConfig
from edgelab.live.sources import ReplaySource
from edgelab.models import EURUSD
from edgelab.risk import RiskConfig
from edgelab.strategies import make_strategy
from edgelab.strategies.base import Strategy

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from conftest import NO_COST, TEST_INSTRUMENT, make_df


class PrefixScripted(Strategy):
    """Scripted directions that work when called on a growing history prefix."""
    name = "prefix_scripted"
    defaults = {}

    def __init__(self, directions, stop):
        super().__init__()
        self._d = list(directions)
        self._s = float(stop)

    @property
    def warmup_bars(self) -> int:
        return 0

    def signals(self, df):
        d = self._d[: len(df)]
        return pd.DataFrame({"direction": d, "stop_distance": self._s}, index=df.index)


def test_basket_runs_all_legs_and_emits_account_equity():
    _, cost, _ = load_config()
    risk = RiskConfig(risk_per_trade_pct=1.0, max_leverage=50)
    dfs = {"EURUSD": trending(n=3000, seed=1), "GBPUSD": trending(n=3000, seed=2)}
    sources, legs, brokers = [], [], []
    for name, df in dfs.items():
        b = PaperBroker(cost, 500.0)
        src = ReplaySource(df, start_index=1)
        legs.append(DemoRunner(src, b, make_strategy("ema_trend", fast=20, slow=100), risk, EURUSD,
                               cfg=RunnerConfig(lookback=len(df)), name=name))
        sources.append(src); brokers.append(b)

    events = []
    BasketRunner(ReplayClock(sources), legs,
                 equity_fn=lambda: sum(b.equity() for b in brokers),
                 on_event=lambda k, d: events.append((k, d))).run()

    assert any(k == "account" for k, _ in events)
    assert any(k == "bar" and d.get("symbol") == "EURUSD" for k, d in events)
    assert any(k == "bar" and d.get("symbol") == "GBPUSD" for k, d in events)
    assert sum(len(b.trades) for b in brokers) > 0


def test_account_kill_switch_flattens_every_leg():
    # Two legs forced long into a steadily falling market; the account drawdown limit must trip and
    # flatten BOTH legs, even though each leg's own limits are disabled.
    n = 40
    opens = [100.0] * 3 + [100.0 - 2.0 * k for k in range(1, n - 2)]
    df = make_df(opens=opens, closes=opens)
    leg_risk = RiskConfig(risk_per_trade_pct=40.0, daily_loss_limit_pct=100.0,
                          max_drawdown_halt_pct=100.0, max_leverage=100.0)
    sources, legs, brokers = [], [], []
    for name in ("A", "B"):
        b = PaperBroker(NO_COST, 1000.0)
        src = ReplaySource(df, start_index=1)
        legs.append(DemoRunner(src, b, PrefixScripted([1] * n, 50.0), leg_risk, TEST_INSTRUMENT,
                               cfg=RunnerConfig(lookback=n), name=name))
        sources.append(src); brokers.append(b)

    events = []
    br = BasketRunner(ReplayClock(sources), legs, equity_fn=lambda: sum(b.equity() for b in brokers),
                      cfg=BasketConfig(account_max_drawdown_pct=10.0, account_daily_loss_pct=100.0),
                      on_event=lambda k, d: events.append((k, d)))
    br.run()

    assert br.stopped_reason is not None and "drawdown" in br.stopped_reason
    assert any(k == "account_kill" for k, _ in events)
    assert all(b.position() is None for b in brokers)


class _ScriptClock:
    """Yields a fixed list of timestamps, then None. Carries a fake client like CTraderClock."""
    def __init__(self, ts_list, client):
        self._ts = list(ts_list)
        self.client = client

    def wait_next(self):
        return self._ts.pop(0) if self._ts else None


class _FailingLeg:
    """A leg whose process() raises the first `fail_times` calls, then succeeds."""
    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.processed = 0
        self.on_event = None
        self.flattened = False
        self.broker = types.SimpleNamespace(position=lambda: None, close_position=lambda r: None)

    def process(self, ts):
        self.processed += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("transient cTrader error")

    def final_flatten(self):
        self.flattened = True


def test_basket_rides_through_transient_error_and_reauths():
    # One leg errors on its first two bars, then recovers. The basket must NOT crash: it should
    # alert, call ensure_authenticated() to re-establish the session, and keep processing.
    reauths = {"n": 0}
    client = types.SimpleNamespace(ensure_authenticated=lambda: reauths.__setitem__("n", reauths["n"] + 1))
    ts = list(pd.date_range("2026-01-01", periods=4, freq="4h", tz="UTC"))
    leg = _FailingLeg(fail_times=2)

    events = []
    BasketRunner(_ScriptClock(ts, client), [leg], equity_fn=lambda: 1000.0,
                 on_event=lambda k, d: events.append((k, d))).run()

    assert leg.processed == 4                         # all four bars attempted, no crash
    assert reauths["n"] == 2                           # re-auth tried once per failed cycle
    assert any(k == "error" for k, _ in events)        # surfaced an alert
    assert any(k == "account" for k, _ in events)      # healthy cycles still ran
    assert leg.flattened                               # clean shutdown


def test_basket_exits_after_too_many_consecutive_errors():
    # A permanently broken leg must eventually exit (SystemExit) so systemd restarts it fresh,
    # rather than spinning forever.
    import pytest
    client = types.SimpleNamespace(ensure_authenticated=lambda: None)
    ts = list(pd.date_range("2026-01-01", periods=20, freq="4h", tz="UTC"))
    leg = _FailingLeg(fail_times=99)
    with pytest.raises(SystemExit):
        BasketRunner(_ScriptClock(ts, client), [leg], equity_fn=lambda: 1000.0,
                     on_event=lambda k, d: None).run()
    assert leg.processed == 5                           # bailed out after 5 straight failures
