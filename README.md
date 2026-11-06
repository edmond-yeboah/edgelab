# edgelab

Market-agnostic research, backtesting and **demo-first** trading framework with hard risk controls.
Same code path for forex, crypto spot, or anything with OHLC candles: only the broker adapter and
cost numbers change.

> Trading involves substantial risk of loss. Nothing here is financial advice or a promise of profit.
> This repo exists to find out, cheaply and honestly, whether a strategy has an edge, and to say
> "no" when it doesn't.

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev,data]"
pytest                                                 # 43 tests, ~1-2 min (synthetic walk-forwards)

# sanity-check the pipeline on synthetic data
python examples/run_baseline.py --synthetic random     # no edge exists -> must REJECT
python examples/run_baseline.py --synthetic trending   # edge exists    -> finds it

# real research data (free, Dukascopy)
python -m edgelab.data.dukascopy EUR_USD 2021-01-01 2026-01-01 --out data/eurusd_h1.parquet
python examples/run_baseline.py --data data/eurusd_h1.parquet --symbol EURUSD
```

## How it fits together

```
candles -> Strategy.signals() -> TradingEngine.on_signal() -> RiskEngine.size() -> Broker
                                   (one shared decision path)     (fail-closed)    PaperBroker | your adapter
```

| Module | Job |
|---|---|
| `models.py` | `Instrument` specs and `CostModel` (spread / fee / slippage in **bps**, so FX and crypto share code) |
| `strategies/` | Signals only (`direction`, `stop_distance`): `ema_trend` + `bollinger_reversion`. Causality enforced by tests |
| `risk.py` | Position sizing, leverage cap, daily loss limit, drawdown **kill switch** (manual reset only) |
| `engine.py` | The one decision path used by backtests **and** demo/live, so they cannot drift apart |
| `brokers/` | `Broker` interface, `PaperBroker` (conservative fills), adapters go here |
| `backtest.py` | Replays bars through the engine; entry = next bar's open, gap-aware stops |
| `validation.py` | Walk-forward, cost-stress, acceptance gates, **single-use holdout** |
| `experiments.py` | Append-only log of every variant tried (trial count keeps you honest) |
| `mode.py` | DEMO by default; LIVE needs an explicit request **and** an env var; mismatch = refuse |
| `live/` | `DemoRunner` (drives the engine bar-by-bar in real time) + data sources (`ReplaySource`, `CTraderSource`) |
| `brokers/ctrader.py` | cTrader Open API adapter (Pepperstone/FP Markets). Reviewed draft; see SETUP_CTRADER.md |
| `data/` | Parquet/CSV storage + validator, Dukascopy downloader, synthetic data for pipeline tests |

## The rules that keep you honest

1. **Write the acceptance numbers in `config/default.yaml` before looking at results.** Never loosen afterwards.
2. **All tuning happens on `dev` data.** The last 20% is a locked holdout, evaluated once.
3. **Every variant you try is logged.** Try 200 and the best one will look good by luck.
4. **Costs are part of the strategy.** Everything is net of spread, fees and slippage, and must survive doubled costs.
5. **Demo checks execution, not edge.** A few months of demo trades is too few to prove profitability; use it
   to compare real fills/spreads against your backtest assumptions and to shake out bugs.
6. **Live is never automatic.** Needs `EDGELAB_ALLOW_LIVE=I_UNDERSTAND_THE_RISK` and a manual decision.

## Findings from building and testing this

- **Sizing on $500 is tight.** At 1:1 leverage a $500 account cannot hold one EURUSD micro lot (~$1,100
  notional), so `max_leverage` defaults to 5. Even then, 0.5% risk ($2.50) cannot buy a 1,000-unit micro lot
  behind a stop wider than ~25 pips; such entries are **rejected** (`below_min_size`) rather than oversized.
  Expect to use ~1% risk or tighter stops. Check your broker's real minimum size.
- **Trade frequency vs. `min_trades: 200`.** The baseline EMA strategy on one H1 pair produces tens of trades
  over several years, so it cannot satisfy a 200-trade gate on its own. Fix this *in advance*: more instruments,
  a faster timeframe, or a different gate, but decide before seeing results.
- **Crypto spot differs mainly in costs.** Exchange fees (~10 bps/side) dominate; set them in `config/default.yaml`.
- **The synthetic "trending" data is unrealistically easy** (profit factors in the dozens). It only proves the
  pipeline can detect an edge when one exists. The random-walk test proves it doesn't invent one
  (0 of 12 seeds passed the gates in testing).
- **`dukascopy-python` 5.0.0 fails to import on Python <= 3.13** (missing `requests`/`DataFeed` names in
  annotations). `data/dukascopy.py` works around it. It was verified against the library's API but **not**
  against the live Dukascopy servers; run it once and eyeball the data.
- Dukascopy prices are not your broker's prices. Validate spreads on the broker's demo account.
- **Re-entry after a stop** is governed by `reentry_cooldown_bars` (default 1): the bot waits one bar,
  then may re-join an ongoing trend even if the signal never flipped. (An earlier version stayed out of
  a trend until the signal changed, which stranded a trend follower for the rest of the move.)

## Live / demo trading

The demo loop reuses the exact engine the backtester validates, so live decisions can't drift from
tested ones. `tests/test_live_runner.py` proves a replay through `DemoRunner` reproduces `run_backtest`
**trade-for-trade** (same entries, prices, P&L, exit reasons).

```bash
# offline dry-run: replay stored history through the full live loop, heartbeat per bar, no network
python examples/run_demo.py --replay data/eurusd_h1.parquet --symbol EURUSD

# live cTrader demo account (after SETUP_CTRADER.md + pip install -e ".[ctrader]")
python examples/run_demo.py --ctrader --symbol EURUSD --timeframe H1
```

**Alerts:** pass `--telegram-heartbeat N` and set `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` and the
runner messages you on entries, exits, flips, stop-outs and the kill switch (via `live/notify.py`).

**Getting cTrader credentials:** `examples/ctrader_get_token.py` exchanges the OAuth redirect code
for an access token and lists your account IDs into a gitignored `.env`. See SETUP_CTRADER.md.

**cTrader adapter status:** implemented against the official SDK and **verified end-to-end** on FP
Markets demo — connect, auth, account balance, symbol lookup and H1 candle decoding all confirmed by
`examples/ctrader_smoke_test.py` (read-only). The order path (`new_market_order` with a server-side
`relativeStopLoss`, `close_position`, stop-out detection) is implemented against the same confirmed
protocol; exercise it with **shadow mode first** (`--ctrader-shadow`: live prices, paper execution,
no real orders), then one real demo order. **SETUP_CTRADER.md** is the full walkthrough.

Run order: `ctrader_smoke_test.py` (done) → `--ctrader-shadow` → `--ctrader` (real demo orders).

## Not built yet (next steps)

1. **Finish the cTrader network calls** (the one integration point above).
2. **Alerts** (e.g. a Telegram bot via the runner's `on_event` hook) and a small dashboard once a strategy survives validation.
3. More strategies: add a class in `strategies/`, register it, and it automatically gets the causality tests.
4. Multi-position / multi-instrument portfolio risk (V1 holds one position at a time).

Note: the Dukascopy downloader can't run from the build sandbox (its data host is outside the network
allowlist). It works from a normal machine — run it locally to pull real history.

## Known limitations (V1)

- P&L assumes quote currency == account currency (fine for EURUSD, GBPUSD, BTCUSD on a USD account).
- One position at a time; market orders only; bar-level (not tick-level) simulation.
- If a bar's range touches the stop, the stop is assumed hit (conservative); no intrabar path modelling.
- Walk-forward windows each restart from the starting equity.
