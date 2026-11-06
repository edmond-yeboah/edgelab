"""cTrader Open API smoke test — READ-ONLY. Places no orders.

Proves, on your own machine (which can reach cTrader, unlike the build sandbox):
  1. TLS connect to demo.ctraderapi.com:5035
  2. application auth (client id/secret) + account auth (access token + account id)
  3. read the account balance
  4. resolve the EURUSD symbol id
  5. fetch recent H1 candles and decode them

Run:
    pip install ctrader-open-api twisted service_identity
    export CTRADER_CLIENT_ID=...  CTRADER_CLIENT_SECRET=...
    export CTRADER_ACCESS_TOKEN=...  CTRADER_ACCOUNT_ID=49414241
    python examples/ctrader_smoke_test.py

It prints results and exits. If anything fails it prints the error and exits non-zero.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

from twisted.internet import reactor, defer
from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAApplicationAuthReq, ProtoOAAccountAuthReq, ProtoOATraderReq,
    ProtoOASymbolsListReq, ProtoOAGetTrendbarsReq,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOATrendbarPeriod

SCALE = 1e5  # cTrader prices are integers scaled by 10^5

CLIENT_ID = os.environ.get("CTRADER_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("CTRADER_CLIENT_SECRET", "")
ACCESS_TOKEN = os.environ.get("CTRADER_ACCESS_TOKEN", "")
ACCOUNT_ID = int(os.environ.get("CTRADER_ACCOUNT_ID", "0") or 0)
SYMBOL = os.environ.get("CTRADER_SYMBOL", "EURUSD")

_missing = [k for k in ("CTRADER_CLIENT_ID", "CTRADER_CLIENT_SECRET", "CTRADER_ACCESS_TOKEN")
            if not os.environ.get(k)] + ([] if ACCOUNT_ID else ["CTRADER_ACCOUNT_ID"])
if _missing:
    print("Missing env vars:", _missing); sys.exit(2)

client = Client(EndPoints.PROTOBUF_DEMO_HOST, EndPoints.PROTOBUF_PORT, TcpProtocol)
_exit_code = {"v": 1}


def fail(stage, err):
    print(f"\n[FAIL] {stage}: {err}")
    _stop()


def _stop():
    if reactor.running:
        reactor.callLater(0.1, reactor.stop)


def _norm(name: str) -> str:
    return "".join(ch for ch in name.upper() if ch.isalnum())


@defer.inlineCallbacks
def run(_):
    try:
        print(f"[1/5] connected to {EndPoints.PROTOBUF_DEMO_HOST}:{EndPoints.PROTOBUF_PORT}")

        app = ProtoOAApplicationAuthReq(); app.clientId = CLIENT_ID; app.clientSecret = CLIENT_SECRET
        yield client.send(app)
        print("[2/5] application auth OK")

        acc = ProtoOAAccountAuthReq(); acc.ctidTraderAccountId = ACCOUNT_ID; acc.accessToken = ACCESS_TOKEN
        yield client.send(acc)
        print(f"[2/5] account auth OK (ctidTraderAccountId={ACCOUNT_ID})")

        tr = ProtoOATraderReq(); tr.ctidTraderAccountId = ACCOUNT_ID
        trader = Protobuf.extract((yield client.send(tr))).trader
        bal = trader.balance / (10 ** trader.moneyDigits)
        print(f"[3/5] balance = {bal:.2f} (login {trader.traderLogin}, leverage 1:{trader.leverageInCents // 100})")

        sl = ProtoOASymbolsListReq(); sl.ctidTraderAccountId = ACCOUNT_ID
        symbols = Protobuf.extract((yield client.send(sl))).symbol
        match = next((s for s in symbols if _norm(s.symbolName) == _norm(SYMBOL)), None)
        if match is None:
            raise RuntimeError(f"symbol {SYMBOL} not found among {len(symbols)} symbols")
        symbol_id = match.symbolId
        print(f"[4/5] {SYMBOL} -> symbolId={symbol_id}")

        count = 10
        now_ms = int(time.time() * 1000)
        tb = ProtoOAGetTrendbarsReq()
        tb.ctidTraderAccountId = ACCOUNT_ID
        tb.symbolId = symbol_id
        tb.period = ProtoOATrendbarPeriod.Value("H1")
        tb.toTimestamp = now_ms
        tb.fromTimestamp = now_ms - (count + 5) * 3600 * 1000
        tb.count = count
        bars = Protobuf.extract((yield client.send(tb))).trendbar
        print(f"[5/5] got {len(bars)} H1 candles. Last {min(5, len(bars))}:")
        for b in bars[-5:]:
            low = b.low
            o = (low + b.deltaOpen) / SCALE
            h = (low + b.deltaHigh) / SCALE
            c = (low + b.deltaClose) / SCALE
            lo = low / SCALE
            ts = datetime.fromtimestamp(b.utcTimestampInMinutes * 60, tz=timezone.utc)
            print(f"   {ts:%Y-%m-%d %H:%M}  O={o:.5f} H={h:.5f} L={lo:.5f} C={c:.5f} vol={b.volume}")

        print("\n[OK] cTrader connection + data path verified.")
        _exit_code["v"] = 0
    except Exception as e:
        fail("run", e)
    finally:
        _stop()


client.setConnectedCallback(lambda c: run(None))
client.setDisconnectedCallback(lambda c, reason: print(f"disconnected: {reason}"))
client.startService()
reactor.callLater(30, lambda: (print("\n[FAIL] timeout after 30s"), _stop()))
reactor.run()
sys.exit(_exit_code["v"])
