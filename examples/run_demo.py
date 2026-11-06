"""Run the strategy in demo mode.

Two ways to run:

  # 1. Offline dry-run: replay a stored file through the full live loop (no broker, no network).
  #    Proves the wiring end-to-end and prints a heartbeat per bar.
  python examples/run_demo.py --replay data/eurusd_h1.parquet --symbol EURUSD

  # 2. SHADOW on live cTrader data (RECOMMENDED FIRST): paper-trades on real demo prices,
  #    places NO real orders. Read-only cTrader access. Run this before ever enabling orders.
  python examples/run_demo.py --ctrader-shadow --symbol EURUSD --timeframe H1

  # 3. Live cTrader demo — places REAL demo orders (after shadow looks right):
  python examples/run_demo.py --ctrader --symbol EURUSD --timeframe H1

Credentials come from the environment, never the command line:
  CTRADER_CLIENT_ID, CTRADER_CLIENT_SECRET, CTRADER_ACCESS_TOKEN, CTRADER_ACCOUNT_ID
Live orders additionally require EDGELAB_ALLOW_LIVE=I_UNDERSTAND_THE_RISK and --host live.
"""

from __future__ import annotations

import argparse
import json
import logging
import os

from edgelab.config import load_config
from edgelab.data.storage import load_candles
from edgelab.live.runner import DemoRunner, RunnerConfig
from edgelab.live.sources import ReplaySource
from edgelab.mode import resolve_mode
from edgelab.models import BTCUSD, EURUSD, GBPUSD
from edgelab.strategies import make_strategy

INSTRUMENTS = {"EURUSD": EURUSD, "GBPUSD": GBPUSD, "BTCUSD": BTCUSD}
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")


def make_on_event(telegram_heartbeat: int):
    """Print every event as JSON, and also push alerts to Telegram if configured in env."""
    from edgelab.live.notify import from_env
    notifier = from_env(os.environ, heartbeat_every=telegram_heartbeat)
    if notifier:
        print("telegram alerts: ON")

    def on_event(kind: str, data: dict) -> None:
        print(json.dumps({"event": kind, **data}))
        if notifier:
            notifier(kind, data)
    return on_event


def main() -> None:
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--replay", help="path to a stored candle file for an offline dry-run")
    src.add_argument("--ctrader-shadow", action="store_true",
                     help="live cTrader DATA, paper execution — no real orders")
    src.add_argument("--ctrader", action="store_true", help="live cTrader account — places REAL demo orders")
    ap.add_argument("--symbol", default="EURUSD", choices=sorted(INSTRUMENTS))
    ap.add_argument("--timeframe", default="H1")
    ap.add_argument("--host", default="demo", choices=["demo", "live"])
    ap.add_argument("--equity", type=float, default=500.0)
    ap.add_argument("--fast", type=int, default=20)
    ap.add_argument("--slow", type=int, default=100)
    ap.add_argument("--max-bars", type=int, default=None)
    ap.add_argument("--telegram-heartbeat", type=int, default=0,
                    help="send a routine Telegram update every N bars (0 = only trades/alerts)")
    a = ap.parse_args()
    on_event = make_on_event(a.telegram_heartbeat)

    instrument = INSTRUMENTS[a.symbol]
    risk_cfg, cost, _ = load_config(asset_class=instrument.asset_class)
    strat = make_strategy("ema_trend", fast=a.fast, slow=a.slow)
    rc = RunnerConfig(lookback=max(1500, strat.warmup_bars * 3), max_bars=a.max_bars)

    if a.replay:
        from edgelab.brokers.paper import PaperBroker
        df = load_candles(a.replay)
        runner = DemoRunner(ReplaySource(df), PaperBroker(cost, a.equity), strat, risk_cfg,
                            instrument, cfg=rc, on_event=on_event)
        runner.run()
        print("dry-run complete")
        return

    # Live cTrader paths (shadow or real)
    mode = resolve_mode(a.host)   # raises unless EDGELAB_ALLOW_LIVE is set for host=live
    from edgelab.brokers.ctrader import CTraderClient, CTraderConfig
    from edgelab.live.sources import CTraderSource
    required = ["CTRADER_CLIENT_ID", "CTRADER_CLIENT_SECRET", "CTRADER_ACCESS_TOKEN", "CTRADER_ACCOUNT_ID"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"Missing env vars: {missing}. See SETUP_CTRADER.md.")
    cfg = CTraderConfig(os.environ["CTRADER_CLIENT_ID"], os.environ["CTRADER_CLIENT_SECRET"],
                        os.environ["CTRADER_ACCESS_TOKEN"], int(os.environ["CTRADER_ACCOUNT_ID"]),
                        host_type=mode.value)
    client = CTraderClient(cfg)
    client.connect()
    source = CTraderSource(client, a.symbol, a.timeframe)

    if a.ctrader_shadow:
        from edgelab.brokers.paper import PaperBroker
        print("SHADOW mode: live cTrader prices, paper execution — NO real orders will be placed.")
        broker = PaperBroker(cost, a.equity)
        runner = DemoRunner(source, broker, strat, risk_cfg, instrument, cfg=rc, on_event=on_event)
    else:
        from edgelab.brokers.ctrader import CTraderBroker
        print("LIVE cTrader mode: REAL demo orders will be placed.")
        broker = CTraderBroker(client, a.symbol, instrument, cost)
        runner = DemoRunner(source, broker, strat, risk_cfg, instrument, cfg=rc,
                            account_is_live=client.account_is_live(), on_event=on_event)
    runner.run()


if __name__ == "__main__":
    main()
