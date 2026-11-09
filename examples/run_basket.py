"""Run the H4 trend BASKET across EUR/USD, GBP/USD, gold and BTC/USD on your cTrader demo.

Shadow first (live prices, paper fills, NO real orders), then real demo orders:

    python examples/run_basket.py --shadow
    python examples/run_basket.py --live        # places REAL demo orders

Credentials from env (CTRADER_CLIENT_ID / _SECRET / _ACCESS_TOKEN / _ACCOUNT_ID).

NOTE on sizing: H4 ATR stops are wide, so a small balance can't meet lot minimums and trades get
rejected (you'll see positions stay null). For a meaningful demo, set your FP Markets DEMO balance
high (e.g. 100,000 virtual — it's free) so the basket can size all four markets.
"""

from __future__ import annotations

import argparse
import json
import logging
import os

from edgelab.config import load_config
from edgelab.live.basket import BasketConfig, BasketRunner, CTraderClock
from edgelab.live.runner import DemoRunner, RunnerConfig
from edgelab.live.sources import CTraderSource
from edgelab.models import Instrument
from edgelab.strategies import make_strategy

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

SYMBOLS = ["EURUSD", "GBPUSD", "XAUUSD", "BTCUSD"]
ASSET = {"BTCUSD": "crypto"}  # everything else = fx


def main() -> None:
    ap = argparse.ArgumentParser()
    m = ap.add_mutually_exclusive_group(required=True)
    m.add_argument("--shadow", action="store_true", help="paper fills on live prices, NO real orders")
    m.add_argument("--live", action="store_true", help="place REAL demo orders")
    ap.add_argument("--symbols", nargs="+", default=SYMBOLS)
    ap.add_argument("--timeframe", default="H1")
    ap.add_argument("--risk-pct", type=float, default=0.5, help="risk per trade, % of account equity")
    ap.add_argument("--equity", type=float, default=100000.0, help="shadow starting equity per account")
    ap.add_argument("--account-max-dd", type=float, default=20.0, help="account kill-switch drawdown %")
    ap.add_argument("--max-bars", type=int, default=None)
    a = ap.parse_args()

    req = ["CTRADER_CLIENT_ID", "CTRADER_CLIENT_SECRET", "CTRADER_ACCESS_TOKEN", "CTRADER_ACCOUNT_ID"]
    missing = [k for k in req if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"Missing env vars: {missing}. See SETUP_CTRADER.md.")

    from edgelab.brokers.ctrader import CTraderClient, CTraderConfig
    cfg = CTraderConfig(os.environ["CTRADER_CLIENT_ID"], os.environ["CTRADER_CLIENT_SECRET"],
                        os.environ["CTRADER_ACCESS_TOKEN"], int(os.environ["CTRADER_ACCOUNT_ID"]),
                        host_type="demo",   # basket is demo-only by design
                        refresh_token=os.environ.get("CTRADER_REFRESH_TOKEN", ""),
                        token_store=os.environ.get("CTRADER_TOKEN_STORE", ".ctrader_refresh_token"))
    client = CTraderClient(cfg)
    client.connect()
    print(("SHADOW" if a.shadow else "LIVE") + " basket:", a.symbols,
          "— NO real orders" if a.shadow else "— REAL demo orders")

    from edgelab.live.notify import from_env
    notifier = from_env(os.environ)   # Telegram if TELEGRAM_BOT_TOKEN / _CHAT_ID are set, else None
    if notifier:
        print("telegram alerts: ON")

    def on_event(kind: str, data: dict) -> None:
        print(json.dumps({"event": kind, **data}))
        if notifier:
            notifier(kind, data)

    from edgelab.risk import RiskConfig
    risk_cfg = RiskConfig(risk_per_trade_pct=a.risk_pct, daily_loss_limit_pct=100.0,
                          max_drawdown_halt_pct=100.0, max_leverage=500.0)  # account backstop does the halting
    rc = RunnerConfig(lookback=max(1500, 150 * 3), max_bars=a.max_bars)

    legs, paper_brokers = [], []
    for sym in a.symbols:
        asset = ASSET.get(sym, "fx")
        _, cost, _ = load_config(asset_class=asset)
        sid = client.symbol_id(sym)
        meta = client._ensure_symbol_meta(sid)           # real venue min/step (centi-units → units)
        instrument = Instrument(sym, asset, min_qty=max(meta["minVolume"] / 100.0, 1e-9),
                                qty_step=max(meta["stepVolume"] / 100.0, 1e-9))
        source = CTraderSource(client, sym, a.timeframe)
        strat = make_strategy("ema_trend", fast=30, slow=150, atr_mult=2.0)
        if a.shadow:
            from edgelab.brokers.paper import PaperBroker
            broker = PaperBroker(cost, a.equity / len(a.symbols))
            paper_brokers.append(broker)
            legs.append(DemoRunner(source, broker, strat, risk_cfg, instrument, cfg=rc, name=sym))
        else:
            from edgelab.brokers.ctrader import CTraderBroker
            broker = CTraderBroker(client, sym, instrument, cost)
            legs.append(DemoRunner(source, broker, strat, risk_cfg, instrument, cfg=rc, name=sym,
                                   account_is_live=client.account_is_live()))

    if a.shadow:
        equity_fn = lambda: sum(b.equity() for b in paper_brokers)
    else:
        brokers = [leg.broker for leg in legs]
        equity_fn = lambda: client.balance() + sum(b.equity() - client.balance() for b in brokers)

    clock = CTraderClock(client, a.timeframe, ref_symbol=a.symbols[0])
    clock.max_bars = a.max_bars
    startup_positions = {}
    for leg in legs:
        p = leg.broker.position()
        startup_positions[leg.name] = None if p is None else {"dir": p.direction, "qty": p.qty}
    on_event("started", {"host": cfg.host_type, "equity": round(equity_fn(), 2),
                         "symbols": a.symbols, "positions": startup_positions})
    BasketRunner(clock, legs, equity_fn,
                 cfg=BasketConfig(account_max_drawdown_pct=a.account_max_dd), on_event=on_event).run()


if __name__ == "__main__":
    main()
