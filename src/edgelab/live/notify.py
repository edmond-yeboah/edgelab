"""Alerts for the demo/live runner, wired via DemoRunner's `on_event` hook.

Design: a Notifier turns engine events into short messages and sends them through a transport.
The transport is injectable so tests use a fake one (no network). The Telegram transport uses
only the stdlib. Routine per-bar heartbeats are throttled; important events (entries, stop-outs,
kill switch, daily halt) always go through.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Callable, Protocol

log = logging.getLogger("edgelab.notify")

ALWAYS_SEND = {"kill_switch", "daily_halt", "entry", "exit", "error"}


class Transport(Protocol):
    def send(self, text: str) -> None: ...


class LoggingTransport:
    """Fallback transport: just logs. Useful for dry-runs and tests."""
    def __init__(self):
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)
        log.info("ALERT %s", text)


class TelegramTransport:
    def __init__(self, bot_token: str, chat_id: str, timeout: float = 10.0,
                 opener: Callable[[str, bytes], None] | None = None):
        if not bot_token or not chat_id:
            raise ValueError("Telegram bot_token and chat_id are required")
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat = chat_id
        self._timeout = timeout
        self._opener = opener  # injectable for tests

    def send(self, text: str) -> None:
        data = urllib.parse.urlencode({"chat_id": self._chat, "text": text}).encode()
        if self._opener is not None:
            self._opener(self._url, data)
            return
        try:
            req = urllib.request.Request(self._url, data=data)
            with urllib.request.urlopen(req, timeout=self._timeout):
                pass
        except Exception as e:  # never let an alert failure crash the trading loop
            log.warning("telegram send failed: %s", e)


class Notifier:
    def __init__(self, transport: Transport, heartbeat_every: int = 0):
        """heartbeat_every: send a routine 'bar' update at most once per this many bars (0 = never)."""
        self.transport = transport
        self.heartbeat_every = heartbeat_every
        self._bars = 0
        self._last_position: dict[str, dict | None] = {}   # per symbol (basket-safe)
        self._last_daily = 0.0                             # last daily-status send (epoch secs)

    def __call__(self, kind: str, data: dict) -> None:
        msg = self._format(kind, data)
        if msg is None:
            return
        self.transport.send(msg)

    def _format(self, kind: str, data: dict) -> str | None:
        if kind == "bar":
            self._bars += 1
            sym = data.get("symbol", "")
            pos = data.get("position")
            # Detect entry/exit/flip per symbol by watching the position field change between bars.
            if pos != self._last_position.get(sym):
                changed = self._position_change_msg(self._last_position.get(sym), pos, data, sym)
                self._last_position[sym] = pos
                if changed:
                    return changed
            if self.heartbeat_every and self._bars % self.heartbeat_every == 0:
                tag = f"{sym} " if sym else ""
                return f"⏳ {tag}{data.get('ts')} equity=${data.get('equity')} pos={pos}"
            return None
        if kind == "account":
            # Once-a-day account summary, so you know it's alive even on a quiet/closed day.
            now = time.time()
            if now - self._last_daily >= 86400:
                self._last_daily = now
                ks = " ⚠️ KILL-SWITCH ON" if data.get("kill_switch") else ""
                return f"📊 Daily status: equity=${data.get('equity')}{ks} — bot running."
            return None
        if kind == "started":
            syms = ", ".join(data.get("symbols", [])) or "?"
            return (f"✅ edgelab started — connected to {data.get('host', 'demo')} "
                    f"(equity=${data.get('equity')}). Watching: {syms}.")
        if kind == "recovered":
            return "🔄 Reconnected to cTrader after an error — back to normal."
        if kind == "kill_switch":
            return f"🛑 KILL SWITCH: {data.get('reason')} @ {data.get('ts')}. Trading halted."
        if kind == "account_kill":
            return f"🛑 ACCOUNT KILL: {data.get('reason')} — equity=${data.get('equity')}. All flattened, stopped."
        if kind == "error":
            return f"⚠️ Error: {data.get('detail')}"
        return None

    def _position_change_msg(self, old, new, data, sym="") -> str | None:
        ts, eq = data.get("ts"), data.get("equity")
        tag = f"{sym} " if sym else ""
        # realized P&L of a trade that just closed this bar (if any)
        closed = data.get("closed") or []
        pnl_str, reason = "", ""
        if closed:
            last = closed[-1]
            reason = last.get("reason", "")
            pnl = last.get("pnl")
            if pnl is not None:
                sign = "+" if pnl >= 0 else ""
                emoji = "🟢" if pnl >= 0 else "🔴"
                pnl_str = f" {emoji} P&L {sign}{pnl:.2f}"
        if old is None and new is not None:
            side = "LONG" if new["dir"] > 0 else "SHORT"
            return f"📈 {tag}ENTRY {side} qty={new['qty']} @ {ts} equity=${eq}"
        if old is not None and new is None:
            rtag = f" ({reason})" if reason else ""
            return f"📉 {tag}EXIT{rtag}{pnl_str} @ {ts} equity=${eq}"
        if old is not None and new is not None and old.get("dir") != new.get("dir"):
            side = "LONG" if new["dir"] > 0 else "SHORT"
            return f"🔄 {tag}FLIP to {side} qty={new['qty']}{pnl_str} @ {ts} equity=${eq}"
        return None


def from_env(env: dict, heartbeat_every: int = 0) -> Notifier | None:
    """Build a Telegram Notifier from env vars, or None if not configured."""
    tok, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return None
    return Notifier(TelegramTransport(tok, chat), heartbeat_every=heartbeat_every)
