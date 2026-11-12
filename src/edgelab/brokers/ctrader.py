"""cTrader Open API integration (Pepperstone / FP Markets / any cTrader broker).

Verified against the official `ctrader-open-api` SDK: every protocol message and field used here
was introspected from the installed SDK, and the read path (connect, auth, account, symbols,
trendbars) was confirmed end-to-end against FP Markets demo via examples/ctrader_smoke_test.py.

The ORDER path (new_market_order / close_position) is implemented against the same confirmed
protocol but should be exercised first with ONE manual demo order — run shadow mode
(CTraderSource + PaperBroker) first, which places no real orders.

Safety:
  * Demo/live separation is enforced by the HOST. demo.ctraderapi.com only serves demo accounts,
    so a live account can't be reached from the demo host at all. account_is_live() derives from
    the host, and the runner's assert_account_matches_mode() still guards it.
  * The protective stop is attached to the order via relativeStopLoss (server-side).
  * Every order carries a unique label; reconcile_positions() adopts existing positions on reconnect.

The SDK is Twisted/async; CTraderClient wraps it in a synchronous facade (reactor in a daemon
thread, requests via blockingCallFromThread) so it fits the bar-by-bar runner.
"""

from __future__ import annotations

import logging
import math
import threading
import time
import uuid

import pandas as pd

from ..mode import ExecutionMode, ModeError, assert_account_matches_mode
from ..models import CostModel, Instrument
from .base import Broker, CloseEvent, Position

log = logging.getLogger("edgelab.ctrader")

SCALE = 1e5  # cTrader integer price scaling
PERIOD_NAME = {"M1": "M1", "M5": "M5", "M15": "M15", "M30": "M30", "H1": "H1", "H4": "H4", "D1": "D1"}
PERIOD_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}


class CTraderError(Exception):
    """cTrader replied with ProtoOAErrorRes instead of the expected message.

    Raised by _call so no caller ever reads a field (.trendbar/.symbol/.accessToken) off an error
    object — that used to throw AttributeError and crash the whole process into a restart loop.
    """
    def __init__(self, code: str, description: str = ""):
        self.code = code
        self.description = description
        super().__init__(f"cTrader error {code}: {description}" if description else f"cTrader error {code}")


def _sdk():
    try:
        from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
        from ctrader_open_api.messages import OpenApiMessages_pb2 as M
        from ctrader_open_api.messages import OpenApiModelMessages_pb2 as Mo
        from ctrader_open_api.messages import OpenApiCommonMessages_pb2 as Cm
        return dict(Client=Client, Protobuf=Protobuf, TcpProtocol=TcpProtocol, EndPoints=EndPoints,
                    M=M, Mo=Mo, Cm=Cm)
    except ImportError as e:  # pragma: no cover
        raise ImportError("Install cTrader support: pip install ctrader-open-api twisted service_identity") from e


class CTraderConfig:
    def __init__(self, client_id: str, client_secret: str, access_token: str,
                 account_id: int, host_type: str = "demo",
                 refresh_token: str = "", token_store: str | None = None):
        if host_type not in ("demo", "live"):
            raise ValueError("host_type must be 'demo' or 'live'")
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.account_id = int(account_id)
        self.host_type = host_type
        self.refresh_token = refresh_token
        # optional file to persist the (rotating) refresh token across restarts
        self.token_store = token_store


# ---------------------------------------------------------------------------
# Trendbar decoding (pure; unit-tested)
# ---------------------------------------------------------------------------
def decode_trendbars(bars) -> pd.DataFrame:
    """cTrader trendbar -> OHLC DataFrame. low is the raw price; open/high/close are deltas above it."""
    rows = []
    for b in bars:
        low = b.low
        rows.append({
            "timestamp": pd.to_datetime(b.utcTimestampInMinutes * 60, unit="s", utc=True),
            "open": (low + b.deltaOpen) / SCALE,
            "high": (low + b.deltaHigh) / SCALE,
            "low": low / SCALE,
            "close": (low + b.deltaClose) / SCALE,
            "volume": float(b.volume),
        })
    df = pd.DataFrame(rows)
    if len(df):
        df = df.set_index("timestamp").sort_index()
    return df


class CTraderClient:
    def __init__(self, cfg: CTraderConfig, call_timeout: float = 25.0):
        self.cfg = cfg
        self.call_timeout = call_timeout
        self._s = _sdk()
        self._client = None
        self._connected = threading.Event()
        self._reactor_started = False
        self.symbols: dict[str, int] = {}        # normalized name -> symbolId
        self.symbol_meta: dict[int, dict] = {}   # symbolId -> {minVolume, stepVolume, maxVolume, digits}
        self._spot: dict[int, dict] = {}         # symbolId -> {bid, ask}
        self._exec_events: list = []
        self._last_exec_error: dict[int, str] = {}   # symbolId -> last rejection errorCode
        self._balance: float | None = None
        self._money_digits = 2
        self.last_bar_may_be_open = True

    # ---- connection ----------------------------------------------------
    def connect(self) -> None:
        s = self._s
        from twisted.internet import reactor, task
        host = s["EndPoints"].PROTOBUF_DEMO_HOST if self.cfg.host_type == "demo" else s["EndPoints"].PROTOBUF_LIVE_HOST
        self._reactor = reactor
        self._client = s["Client"](host, s["EndPoints"].PROTOBUF_PORT, s["TcpProtocol"])
        self._client.setConnectedCallback(lambda c: self._connected.set())
        self._client.setDisconnectedCallback(lambda c, reason: log.warning("disconnected: %s", reason))
        self._client.setMessageReceivedCallback(self._on_message)

        if not reactor.running:
            threading.Thread(target=lambda: reactor.run(installSignalHandlers=False), daemon=True).start()
            self._reactor_started = True
        reactor.callFromThread(self._client.startService)
        if not self._connected.wait(self.call_timeout):
            raise ModeError("Timed out connecting to cTrader")

        # authenticate: app auth + (best-effort) token refresh + account auth
        self._authenticate()
        log.info("cTrader connected + authed (account %s, host %s)", self.cfg.account_id, self.cfg.host_type)

        # heartbeat every 10s or the server drops us. Heartbeats get no response, so swallow the
        # SDK's response-timeout errback instead of letting it log an "Unhandled error in Deferred".
        def _heartbeat():
            try:
                d = self._client.send(self._s["Cm"].ProtoHeartbeatEvent())
                d.addErrback(lambda f: None)
            except Exception:
                pass
        hb = task.LoopingCall(_heartbeat)
        reactor.callFromThread(lambda: hb.start(10.0, now=False))

        # refresh the access token daily (it expires in ~30 days) so a long run never dies on expiry.
        if self.cfg.refresh_token:
            from twisted.internet import threads
            rl = task.LoopingCall(lambda: threads.deferToThread(self._refresh_and_reauth))
            reactor.callFromThread(lambda: rl.start(24 * 3600, now=False))

        self._load_symbols()
        self._refresh_balance()

    # ---- token refresh -------------------------------------------------
    def _load_persisted_refresh_token(self) -> None:
        import os
        if self.cfg.token_store and os.path.exists(self.cfg.token_store):
            try:
                tok = open(self.cfg.token_store).read().strip()
                if tok:
                    self.cfg.refresh_token = tok
            except Exception as e:  # pragma: no cover
                log.warning("could not read token store: %s", e)

    def _persist_refresh_token(self, token: str) -> None:
        if self.cfg.token_store:
            try:
                with open(self.cfg.token_store, "w") as f:
                    f.write(token)
            except Exception as e:  # pragma: no cover
                log.warning("could not write token store: %s", e)

    def _authenticate(self) -> None:
        """App auth, then best-effort token refresh, then account auth with the best token available.

        Used on first connect and on reconnect. A failed refresh no longer blocks auth: we fall back
        to the current access token, so a transient refresh error doesn't lock the bot out.
        """
        self._retry_call(lambda: self._req("ProtoOAApplicationAuthReq", clientId=self.cfg.client_id,
                                           clientSecret=self.cfg.client_secret), "application auth")
        self._load_persisted_refresh_token()
        if self.cfg.refresh_token:
            self._try_refresh_token()   # best-effort; updates access_token on success
        # account auth can transiently fail (e.g. CANT_ROUTE_REQUEST) on a flaky/just-woken server
        self._retry_call(lambda: self._req("ProtoOAAccountAuthReq", ctidTraderAccountId=self.cfg.account_id,
                                           accessToken=self.cfg.access_token), "account auth")

    def _retry_call(self, make_msg, what: str, attempts: int = 4, delay: float = 3.0):
        """Send a request, retrying a few times on a transient server error before giving up.
        A fresh message is built each attempt. Raises the last error if all attempts fail."""
        last = None
        for _ in range(attempts):
            try:
                return self._call(make_msg())
            except Exception as e:
                last = e
                log.warning("%s failed (%s); retrying in %ss", what, e, delay)
                time.sleep(delay)
        raise last

    def _try_refresh_token(self) -> bool:
        """Exchange the refresh token for a fresh access token. Returns True on success; on failure
        logs and returns False (never raises) so auth can fall back to the existing access token."""
        try:
            res = self._call(self._req("ProtoOARefreshTokenReq", refreshToken=self.cfg.refresh_token))
            self.cfg.access_token = res.accessToken
            if getattr(res, "refreshToken", ""):
                self.cfg.refresh_token = res.refreshToken
                self._persist_refresh_token(res.refreshToken)
            log.info("access token refreshed (expires in ~%ss)", getattr(res, "expiresIn", "?"))
            return True
        except Exception as e:
            log.warning("token refresh failed (keeping current access token): %s", e)
            return False

    def _refresh_and_reauth(self) -> None:
        """Daily token refresh + re-auth. Best-effort; never raises into the reactor loop."""
        try:
            if self._try_refresh_token():
                self._call(self._req("ProtoOAAccountAuthReq", ctidTraderAccountId=self.cfg.account_id,
                                     accessToken=self.cfg.access_token))
        except Exception as e:
            log.warning("re-auth after refresh failed (will retry next cycle): %s", e)

    def ensure_authenticated(self) -> None:
        """Re-establish the session after a disconnect/error: re-run auth and re-subscribe spots.

        Called from the runner's error handler so a dropped connection self-heals by the next bar
        instead of needing a process restart. Raises if the socket can't be re-authed."""
        if not self._connected.wait(self.call_timeout):
            raise ModeError("cTrader not connected")
        self._authenticate()
        for sid in list(self._spot.keys()):
            try:
                self._call(self._req("ProtoOASubscribeSpotsReq",
                                     ctidTraderAccountId=self.cfg.account_id, symbolId=[sid]))
            except Exception:
                pass
        log.info("cTrader session re-authenticated")

    def account_is_live(self) -> bool:
        # The demo host serves only demo accounts; auth above would have failed otherwise.
        return self.cfg.host_type == "live"

    # ---- requests ------------------------------------------------------
    def _req(self, name: str, **fields):
        msg = getattr(self._s["M"], name)()
        for k, v in fields.items():
            fd = msg.DESCRIPTOR.fields_by_name[k]
            if fd.label == fd.LABEL_REPEATED:
                getattr(msg, k).extend(v)   # repeated fields can't be assigned, only extended
            else:
                setattr(msg, k, v)
        return msg

    def _call(self, msg):
        """Send a request from the main thread and block for its response.

        Uses call_timeout (not the SDK's 5s default) so a large trendbar fetch has time to return.
        """
        from twisted.internet import threads
        resp = threads.blockingCallFromThread(
            self._reactor, self._client.send, msg, None, self.call_timeout)
        extracted = self._s["Protobuf"].extract(resp)
        if type(extracted).__name__ == "ProtoOAErrorRes":
            raise CTraderError(str(getattr(extracted, "errorCode", "?")),
                               str(getattr(extracted, "description", "")))
        return extracted

    def _on_message(self, client, message):
        try:
            payload = self._s["Protobuf"].extract(message)
        except Exception:
            return
        tn = type(payload).__name__
        if tn == "ProtoOASpotEvent":
            d = self._spot.setdefault(payload.symbolId, {})
            if payload.bid:
                d["bid"] = payload.bid / SCALE
            if payload.ask:
                d["ask"] = payload.ask / SCALE
            d["ts"] = time.time()   # last-tick time: lets us tell an open market from a closed one
        elif tn == "ProtoOAExecutionEvent":
            self._exec_events.append(payload)
            # Diagnostic: log the venue's execution type + error code for each order, so a rejected
            # order (e.g. gold/BTC bouncing) tells us exactly WHY (lot size, trading disabled, etc.).
            try:
                et = getattr(payload, "executionType", None)
                ec = getattr(payload, "errorCode", "") or ""
                order = getattr(payload, "order", None)
                sid = getattr(getattr(order, "tradeData", None), "symbolId", None)
                sym = next((n for n, i in self.symbols.items() if i == sid), sid)
                log.info("exec event: type=%s symbol=%s errorCode=%s", et, sym, ec)
                if ec and sid is not None:
                    self._last_exec_error[sid] = ec
            except Exception:
                pass

    # ---- reference data ------------------------------------------------
    def _load_symbols(self) -> None:
        # detailed specs (volume steps) for the symbols we will trade are fetched on demand.
        # Retry a few times so a brief server blip at startup doesn't fail the whole connect.
        last = None
        for _ in range(3):
            try:
                res = self._call(self._req("ProtoOASymbolsListReq", ctidTraderAccountId=self.cfg.account_id))
                for sym in res.symbol:
                    norm = "".join(ch for ch in sym.symbolName.upper() if ch.isalnum())
                    self.symbols[norm] = sym.symbolId
                return
            except Exception as e:
                last = e
                time.sleep(2)
        raise last

    def symbol_id(self, name: str) -> int:
        norm = "".join(ch for ch in name.upper() if ch.isalnum())
        if norm not in self.symbols:
            raise KeyError(f"symbol {name} not available on this account")
        return self.symbols[norm]

    def _ensure_symbol_meta(self, symbol_id: int) -> dict:
        if symbol_id not in self.symbol_meta:
            res = self._call(self._req("ProtoOASymbolByIdReq", ctidTraderAccountId=self.cfg.account_id,
                                       symbolId=[symbol_id]))
            s = res.symbol[0]
            self.symbol_meta[symbol_id] = {"minVolume": s.minVolume, "stepVolume": s.stepVolume,
                                           "maxVolume": s.maxVolume, "digits": s.digits}
        return self.symbol_meta[symbol_id]

    def _refresh_balance(self) -> None:
        res = self._call(self._req("ProtoOATraderReq", ctidTraderAccountId=self.cfg.account_id))
        self._money_digits = res.trader.moneyDigits
        self._balance = res.trader.balance / (10 ** self._money_digits)

    # ---- market data ---------------------------------------------------
    def get_trendbars(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        sid = self.symbol_id(symbol)
        minutes = PERIOD_MINUTES[timeframe]
        now_ms = int(time.time() * 1000)
        res = self._call(self._req(
            "ProtoOAGetTrendbarsReq", ctidTraderAccountId=self.cfg.account_id, symbolId=sid,
            period=self._s["Mo"].ProtoOATrendbarPeriod.Value(timeframe),
            fromTimestamp=now_ms - (count + 5) * minutes * 60 * 1000, toTimestamp=now_ms, count=count))
        return decode_trendbars(res.trendbar)

    def wait_next_closed_bar(self, symbol: str, timeframe: str,
                             on_poll=None, poll_interval: float = 0.0) -> pd.Timestamp | None:
        minutes = PERIOD_MINUTES[timeframe]
        period = pd.Timedelta(minutes=minutes)
        boundary = pd.Timestamp.utcnow().ceil(period)
        # +3s buffer so the just-closed bar is finalized server-side before we fetch
        deadline = time.time() + max(0.0, (boundary - pd.Timestamp.utcnow()).total_seconds()) + 3.0
        if on_poll and poll_interval and poll_interval > 0:
            # Sleep in chunks, running the watch between them so a mid-bar stop-out is seen within
            # ~poll_interval seconds instead of up to a whole bar later.
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                time.sleep(min(poll_interval, remaining))
                if time.time() < deadline:   # skip the poll right at the boundary — the bar cycle handles it
                    on_poll()
        else:
            time.sleep(max(0.0, deadline - time.time()))
        return boundary

    def subscribe_spots(self, symbol: str) -> None:
        sid = self.symbol_id(symbol)
        self._call(self._req("ProtoOASubscribeSpotsReq", ctidTraderAccountId=self.cfg.account_id, symbolId=[sid]))

    def mid_price(self, symbol: str) -> float | None:
        d = self._spot.get(self.symbol_id(symbol))
        if d and "bid" in d and "ask" in d:
            return (d["bid"] + d["ask"]) / 2
        return None

    def is_market_open(self, symbol: str, max_age: float = 300.0) -> bool:
        """True if a live price tick arrived within max_age seconds. A closed market stops ticking,
        so a stale or missing last tick means 'closed' — a broker-agnostic way to avoid firing
        orders on weekends/holidays that can't fill (covers holidays too, not just weekends)."""
        try:
            d = self._spot.get(self.symbol_id(symbol))
        except KeyError:
            return False
        if not d or "ts" not in d:
            return False
        return (time.time() - d["ts"]) < max_age

    def last_exec_error(self, symbol: str) -> str:
        """Most recent execution-event errorCode for this symbol (why an order was rejected), or ''."""
        try:
            return self._last_exec_error.get(self.symbol_id(symbol), "")
        except Exception:
            return ""

    # ---- trading -------------------------------------------------------
    def volume_from_units(self, symbol_id: int, units: float) -> int:
        """Convert base-currency units to cTrader volume (centi-units), snapped to the symbol step."""
        meta = self._ensure_symbol_meta(symbol_id)
        vol = units * 100.0
        step = meta["stepVolume"] or 1
        vol = math.floor(vol / step) * step
        return int(max(meta["minVolume"], min(vol, meta["maxVolume"])))

    def relative_stop_from_distance(self, symbol_id: int, stop_distance: float) -> int:
        """Convert a price-unit stop distance to cTrader's relativeStopLoss (1/100000 of a price
        unit), snapped to the symbol's price grid.

        cTrader rejects a relativeStopLoss that isn't a whole number of ticks (INVALID_REQUEST).
        FX pairs have digits=5, so any integer is a whole tick. Gold/BTC have digits=2, so the
        value must be a multiple of 10**(5-digits) = 1000 — a raw ATR stop like 64.5197 is not.
        Round to the nearest tick, and never below one tick (a zero stop would be invalid too)."""
        digits = int(self._ensure_symbol_meta(symbol_id)["digits"])
        grid = 10 ** max(0, int(math.log10(SCALE)) - digits)    # SCALE=1e5 -> 1 for 5 digits, 1000 for 2
        steps = max(1, round(stop_distance * SCALE / grid))
        return int(steps * grid)

    def new_market_order(self, symbol: str, direction: int, units: float,
                         stop_distance: float, label: str) -> dict:
        sid = self.symbol_id(symbol)
        side = self._s["Mo"].ProtoOATradeSide.Value("BUY" if direction > 0 else "SELL")
        fields = dict(ctidTraderAccountId=self.cfg.account_id, symbolId=sid,
                      orderType=self._s["Mo"].ProtoOAOrderType.Value("MARKET"), tradeSide=side,
                      volume=self.volume_from_units(sid, units), label=label)
        if stop_distance and stop_distance > 0:
            fields["relativeStopLoss"] = self.relative_stop_from_distance(sid, stop_distance)
        res = self._call(self._req("ProtoOANewOrderReq", **fields))
        # The DIRECT response to a new-order request carries the rejection reason here (it does not
        # come through the unsolicited _on_message path). Capture its errorCode so the rejection
        # alert can name it instead of falling back to the generic message.
        try:
            ec = getattr(res, "errorCode", "") or ""
            et = getattr(res, "executionType", None)
            log.info("new-order response %s: type=%s errorCode=%s", symbol, et, ec)
            if ec:
                self._last_exec_error[sid] = ec
        except Exception:
            pass
        return {"raw": res}

    def close_position(self, position_id: int, volume: int) -> dict:
        return {"raw": self._call(self._req("ProtoOAClosePositionReq",
                ctidTraderAccountId=self.cfg.account_id, positionId=position_id, volume=volume))}

    def reconcile_positions(self) -> list[dict]:
        res = self._call(self._req("ProtoOAReconcileReq", ctidTraderAccountId=self.cfg.account_id))
        out = []
        for p in res.position:
            td = p.tradeData
            out.append({"positionId": p.positionId, "direction": 1 if td.tradeSide == 1 else -1,
                        "volume": td.volume, "entry_price": p.price,
                        "label": td.label, "symbolId": td.symbolId})
        return out

    def pop_exec_events(self) -> list:
        ev, self._exec_events = self._exec_events, []
        return ev

    def balance(self) -> float | None:
        return self._balance


class CTraderBroker(Broker):
    """Real-venue adapter. One position at a time (matches V1)."""

    is_simulated = False

    def __init__(self, client: CTraderClient, symbol: str, instrument: Instrument, cost: CostModel | None = None):
        self.client = client
        self.symbol = symbol
        self.instrument = instrument
        self.cost = cost or CostModel()
        self.mode = ExecutionMode.DEMO if client.cfg.host_type == "demo" else ExecutionMode.LIVE
        self._pos: Position | None = None
        self._events: list[CloseEvent] = []
        self._realized: list[dict] = []
        self._rejections: list[dict] = []
        self._label_prefix = f"edgelab-{uuid.uuid4().hex[:8]}"
        self._n = 0
        assert_account_matches_mode(self.mode, client.account_is_live())
        client.subscribe_spots(symbol)
        for p in client.reconcile_positions():
            if p["symbolId"] == client.symbol_id(symbol):
                self._pos = self._adopt(p)

    def position(self) -> Position | None:
        return self._pos

    def reference_price(self) -> float:
        px = self.client.mid_price(self.symbol)
        if px is None:
            raise RuntimeError("no spot price yet; cannot size order")
        return px

    def equity(self) -> float:
        bal = self.client.balance() or 0.0
        if self._pos is None:
            return bal
        px = self.client.mid_price(self.symbol)
        if px is None:
            return bal
        return bal + self._pos.direction * (px - self._pos.entry_price) * self._pos.qty

    # Hard rejections won't fix themselves on a retry — stop immediately and report them.
    _HARD_REJECTS = {"INVALID_REQUEST", "TRADING_BAD_VOLUME", "TRADING_BAD_PRICES", "NOT_ENOUGH_MONEY",
                     "POSITION_NOT_FOUND", "TRADING_DISABLED", "SHORT_SELLING_NOT_ALLOWED"}

    def submit_market(self, direction: int, qty: float, stop_distance: float, risk_amount: float,
                      attempts: int = 3) -> Position | None:
        """Place a market order and confirm it actually opened a position on the venue.

        Gold/BTC are wider and jumpier than EUR/GBP: a single IMMEDIATE_OR_CANCEL market order can
        come back 0-filled at the exact bar-close instant, even though the identical order fills a
        moment later. So we retry into a fillable moment — but reconcile the symbol BEFORE every
        attempt, so a fill from a previous try is adopted instead of stacked (never a double-fill).
        We confirm by our label OR by any new position on this symbol (we were flat before ordering),
        so a late or oddly-labelled async fill is tracked, never orphaned."""
        if self._pos is not None:
            raise RuntimeError("one position at a time")
        sid = self.client.symbol_id(self.symbol)
        side = "BUY" if direction > 0 else "SELL"
        venue = None

        for attempt in range(1, attempts + 1):
            # Did a prior attempt (or a slow async fill) already open a position on this symbol?
            # We hold at most one per symbol, so adopt it rather than firing again.
            existing = [p for p in self.client.reconcile_positions() if p["symbolId"] == sid]
            if existing:
                venue = existing[0]
                break

            label = self._next_label()
            self.client.new_market_order(self.symbol, direction, qty, stop_distance, label)
            venue = self._confirm_fill(sid, label)
            if venue is not None:
                break

            err = self.client.last_exec_error(self.symbol)
            if err in self._HARD_REJECTS:
                log.warning("order rejected %s %s: %s — not retrying", self.symbol, side, err)
                break
            log.info("order %s %s not filled on attempt %d/%d (%s); retrying",
                     self.symbol, side, attempt, attempts, err or "0-fill, no error")

        if venue is None:
            reason = self.client.last_exec_error(self.symbol) or "not filled after retries"
            log.warning("order NOT filled %s %s qty=%s (%s); staying flat",
                        self.symbol, side, qty, reason)
            self._rejections.append({"symbol": self.symbol, "side": side, "reason": reason})
            return None

        entry_px = venue["entry_price"]   # position price is already in price units (a double), not 1e5-scaled
        filled_qty = venue["volume"] / 100.0 or qty   # trust the venue's actual fill size
        vdir = venue["direction"]
        self._pos = Position(direction=vdir, qty=filled_qty, entry_price=entry_px,
                             stop=entry_px - vdir * stop_distance, entry_time=pd.Timestamp.utcnow(),
                             risk_amount=risk_amount)
        self._pos.label = venue.get("label", "")  # type: ignore[attr-defined]
        self._pos.venue_id = venue["positionId"]  # type: ignore[attr-defined]
        log.info("order FILLED %s %s qty=%s @ %.5f label=%s",
                 self.symbol, side, filled_qty, entry_px, self._pos.label)
        return self._pos

    def _confirm_fill(self, sid: int, label: str, attempts: int = 10, delay: float = 0.5) -> dict | None:
        """Poll the venue for the fill. Match our label first; fall back to ANY position on this
        symbol — we were flat before ordering, so a new one is ours even if the label comes back
        empty or altered. ~5s window (10 x 0.5s) covers a slow async fill."""
        for _ in range(attempts):
            positions = self.client.reconcile_positions()
            for p in positions:
                if p.get("label") == label:
                    return p
            on_sym = [p for p in positions if p["symbolId"] == sid]
            if on_sym:
                return on_sym[0]
            time.sleep(delay)
        return None

    def market_open(self) -> bool:
        """Whether this symbol's market is currently open (live ticks arriving within ~5 min)."""
        return self.client.is_market_open(self.symbol)

    def close_position(self, reason: str) -> None:
        if self._pos is None:
            return
        p = self._pos
        exit_px = self.client.mid_price(self.symbol) or p.entry_price
        pnl = p.direction * (exit_px - p.entry_price) * p.qty     # per-leg realized P&L estimate
        vid = getattr(p, "venue_id", 0)
        vol = self.client.volume_from_units(self.client.symbol_id(self.symbol), p.qty)
        if vid:
            self.client.close_position(vid, vol)
        self._realized.append({"pnl": round(pnl, 2), "reason": reason, "dir": p.direction})
        self._events.append(CloseEvent(p.direction, reason))
        self._pos = None
        self.client._refresh_balance()

    def pop_events(self) -> list[CloseEvent]:
        # surface server-side stop-outs: if an execution event reports our position closed, emit a stop
        for ev in self.client.pop_exec_events():
            try:
                if self._pos is not None and ev.position.positionId == getattr(self._pos, "venue_id", -1) \
                        and ev.position.positionStatus == 2:  # CLOSED
                    p = self._pos
                    exit_px = getattr(p, "stop", float("nan"))
                    if exit_px != exit_px:   # NaN: adopted position with no known stop
                        exit_px = self.client.mid_price(self.symbol) or p.entry_price
                    pnl = p.direction * (exit_px - p.entry_price) * p.qty
                    self._realized.append({"pnl": round(pnl, 2), "reason": "stop", "dir": p.direction})
                    self._events.append(CloseEvent(p.direction, "stop"))
                    self._pos = None
                    self.client._refresh_balance()
            except Exception:
                pass
        ev, self._events = self._events, []
        return ev

    def pop_realized(self) -> list[dict]:
        r, self._realized = self._realized, []
        return r

    def pop_rejections(self) -> list[dict]:
        r, self._rejections = self._rejections, []
        return r

    # ---- helpers -------------------------------------------------------
    def _next_label(self) -> str:
        self._n += 1
        return f"{self._label_prefix}-{self._n}"

    def _adopt_venue_id(self) -> None:
        for p in self.client.reconcile_positions():
            if p.get("label") == getattr(self._pos, "label", None):
                self._pos.venue_id = p["positionId"]  # type: ignore[attr-defined]
                return

    def _adopt(self, p: dict) -> Position:
        pos = Position(direction=p["direction"], qty=p["volume"] / 100.0, entry_price=p["entry_price"],
                       stop=float("nan"), entry_time=pd.Timestamp.utcnow(), risk_amount=0.0)
        pos.venue_id = p["positionId"]  # type: ignore[attr-defined]
        pos.label = p.get("label", "")  # type: ignore[attr-defined]
        return pos
