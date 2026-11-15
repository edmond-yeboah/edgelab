"""Trade journal: a durable record of every bot trade, built for the demo-vs-backtest review.

Two kinds of record are appended to journal/trades.jsonl (one JSON object per line):

  entry  written by the broker when an order fills: fill price, the price at decision time
         (so slippage can be measured), the planned stop and the risk taken.
  close  written from cTrader's own DEAL HISTORY, not from the bot's estimates: real exit price,
         gross P&L, swap, commission, net P&L and balance, joined to the entry by position id.

Closes come from the broker's deal history on purpose. It is the source of truth, it survives
restarts, and it catches stop-outs and manual closes whether or not the bot was watching.
The same sync is what tells you about closes that happened while the bot was down.

journal/state.json remembers how far the deal history has been read and which weekly summary
was last sent, so a restart neither re-announces old closes nor repeats a summary.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger("edgelab.journal")

# What the validated backtest says to expect (basket, out-of-sample 2021-2026, 0.25% risk).
BACKTEST = {"pf": 1.53, "exp_r": 0.33, "trades_per_week": 1.7}
MIN_TRADES_FOR_COMPARISON = 30      # below this, a profit factor or expectancy is mostly noise
SYNC_EVERY = 120.0                  # seconds between deal-history checks while running
OVERLAP_MS = 10 * 60 * 1000         # re-read a little history each time; dedupe by deal id


def _iso(ts_ms_or_dt) -> str:
    if isinstance(ts_ms_or_dt, datetime):
        dt = ts_ms_or_dt
    else:
        dt = datetime.fromtimestamp(ts_ms_or_dt / 1000.0, timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class TradeJournal:
    def __init__(self, directory: str | Path = "journal", clock: Callable[[], float] = time.time):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "trades.jsonl"
        self.state_path = self.dir / "state.json"
        self._clock = clock
        self._last_sync_wall = 0.0
        self._state = self._load_state()
        if "started" not in self._state:
            self._state["started"] = _iso(int(clock() * 1000))
            self._save_state()
        self._seen = {r["deal_id"] for r in self.records("close")}

    # ---- storage ---------------------------------------------------------------------------
    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        try:
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state))
            os.replace(tmp, self.state_path)
        except OSError as e:
            log.error("journal state not saved: %s", e)

    def _append(self, rec: dict) -> None:
        try:
            with open(self.path, "a") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except OSError as e:
            log.error("journal write failed: %s", e)

    def records(self, kind: str | None = None) -> list[dict]:
        out = []
        try:
            with open(self.path) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue                      # a torn last line must not break reading
                    if kind is None or r.get("type") == kind:
                        out.append(r)
        except OSError:
            pass
        return out

    # ---- entries (written by the broker) -----------------------------------------------------
    def record_entry(self, **f) -> dict:
        ref, fill, d = f.get("ref_price"), f.get("fill_price"), f.get("direction")
        slip = None
        if ref and fill and d:
            slip = round(d * (fill - ref) / ref * 1e4, 3)      # bps; positive = filled worse than decision price
        rec = {"type": "entry", "at": _iso(int(self._clock() * 1000)), **f, "slippage_bps": slip}
        self._append(rec)
        return rec

    def seed_open_positions(self, positions: list[dict], risk_amount: float) -> int:
        """Give positions that were already open when the journal started (adopted, no entry record)
        a stand-in entry, so their closes count as bot trades. risk_amount is an ESTIMATE (the bot's
        nominal risk per trade at today's equity); these records are flagged seeded=True."""
        known = {e["position_id"] for e in self.records("entry")}
        n = 0
        for p in positions:
            pid = p.get("position_id")
            if not pid or pid in known:
                continue
            self.record_entry(symbol=p.get("symbol"), position_id=pid, direction=p.get("direction"),
                              qty=p.get("qty"), fill_price=p.get("entry_price"), ref_price=None,
                              risk_amount=round(float(risk_amount), 2), seeded=True)
            n += 1
        return n

    # ---- closes (from the broker's deal history) -----------------------------------------------
    def _close_record(self, d: dict, entry: dict | None) -> dict:
        direction = 1 if d["side"] == "SELL" else -1          # a closing SELL ends a long
        net = d["gross"] + d["swap"] + d["commission"]
        rec = {"type": "close", "at": _iso(d["ts_ms"]), "deal_id": d["deal_id"],
               "position_id": d["position_id"], "symbol": d["symbol"], "direction": direction,
               "qty": (d.get("closed_volume") or d["volume"]) / 100.0,
               "entry_price": d["entry_price"], "exit_price": d["price"],
               "gross": round(d["gross"], 2), "swap": round(d["swap"], 2),
               "commission": round(d["commission"], 2), "net": round(net, 2),
               "balance": d["balance"], "bot": entry is not None}
        if entry is not None:
            risk = entry.get("risk_amount") or 0
            rec["risk_amount"] = risk
            rec["r"] = round(net / risk, 3) if risk else None
            sd, planned = entry.get("stop_distance"), entry.get("planned_stop")
            if sd and planned is not None:
                over = direction * (planned - d["price"]) / sd      # >0: exited worse than the planned stop
                rec["stop_overshoot_r"] = round(over, 3)
                rec["exit_kind"] = "stop" if over > -0.1 else "signal"
        return rec

    def sync(self, client) -> list[dict]:
        """Read new deal history, journal any closing deals not yet recorded, return the new ones.
        The very first call just starts the clock (history before the journal existed is ignored)."""
        now_ms = int(self._clock() * 1000)
        since = self._state.get("last_deal_ms")
        if since is None:
            self._state["last_deal_ms"] = now_ms
            self._save_state()
            return []
        deals = client.deals_since(since - OVERLAP_MS)
        entries = {e["position_id"]: e for e in self.records("entry")}
        latest, new = since, []
        for d in deals:
            latest = max(latest, d["ts_ms"])
            if not d["is_close"] or d["deal_id"] in self._seen:
                continue
            rec = self._close_record(d, entries.get(d["position_id"]))
            self._append(rec)
            self._seen.add(d["deal_id"])
            new.append(rec)
        self._state["last_deal_ms"] = latest
        self._save_state()
        return new

    def startup_sync(self, client, on_event: Callable[[str, dict], None]) -> int:
        """At startup, journal closes that happened while the bot was down and announce them."""
        try:
            new = self.sync(client)
        except Exception as e:
            log.warning("startup deal sync failed: %s", e)
            return 0
        for rec in new:
            on_event("missed_close", rec)
        return len(new)

    # ---- periodic housekeeping -----------------------------------------------------------------
    def tick(self, client, on_event, equity_fn, positions_fn, now: datetime | None = None) -> None:
        """Called every ~90s from the runner. Keeps the journal current and sends the weekly summary.
        Never raises: reporting problems must not be able to disturb trading."""
        wall = self._clock()
        now = now or datetime.fromtimestamp(wall, timezone.utc)
        if wall - self._last_sync_wall >= SYNC_EVERY:
            self._last_sync_wall = wall
            try:
                self.sync(client)
            except Exception as e:
                log.warning("journal sync failed: %s", e)
        if self.weekly_due(now):
            try:
                on_event("weekly", {"text": self.weekly_summary(now, equity_fn(), positions_fn())})
                self.mark_weekly_sent(now)
            except Exception as e:
                log.warning("weekly summary failed: %s", e)

    # ---- weekly summary ------------------------------------------------------------------------
    @staticmethod
    def _week_label(now: datetime) -> str:
        y, w, _ = now.isocalendar()
        return f"{y}-W{w:02d}"

    def weekly_due(self, now: datetime) -> bool:
        """Sunday evening UTC (the FX market is still closed), once per ISO week."""
        return now.weekday() == 6 and now.hour >= 18 and self._state.get("last_weekly") != self._week_label(now)

    def mark_weekly_sent(self, now: datetime) -> None:
        self._state["last_weekly"] = self._week_label(now)
        self._save_state()

    @staticmethod
    def _stats(rows: list[dict]) -> dict:
        nets = [r["net"] for r in rows]
        wins, losses = [n for n in nets if n > 0], [n for n in nets if n <= 0]
        rs = [r["r"] for r in rows if r.get("r") is not None]
        pf = (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None
        return {"n": len(rows), "wins": len(wins), "losses": len(losses), "net": sum(nets),
                "sum_r": sum(rs), "avg_r": (sum(rs) / len(rs)) if rs else None, "pf": pf}

    def weekly_summary(self, now: datetime, equity: float | None, open_positions: dict | None) -> str:
        closes = [c for c in self.records("close") if c.get("bot")]
        entries = self.records("entry")
        week = [c for c in closes if _parse(c["at"]) >= now - timedelta(days=7)]
        w, a = self._stats(week), self._stats(closes)
        started = _parse(self._state["started"]).strftime("%d %b")
        lines = [f"📊 Weekly summary — week ending {now.strftime('%a %d %b')}"]
        if w["n"]:
            lines.append(f"This week: {w['n']} closed ({w['wins']}W/{w['losses']}L), "
                         f"net {w['net']:+,.2f} ({w['sum_r']:+.1f}R)")
        else:
            lines.append("This week: no trades closed.")
        if a["n"]:
            pf = f"{a['pf']:.2f}" if a["pf"] is not None else "n/a"
            avg = f"{a['avg_r']:+.2f}R" if a["avg_r"] is not None else "n/a"
            lines.append(f"Since {started}: {a['n']} closed, net {a['net']:+,.2f}, PF {pf}, avg {avg}/trade")
        slips = [e["slippage_bps"] for e in entries if e.get("slippage_bps") is not None]
        overs = [c["stop_overshoot_r"] for c in closes if c.get("exit_kind") == "stop"
                 and c.get("stop_overshoot_r") is not None]
        bits = []
        if slips:
            bits.append(f"entry slippage avg {sum(slips) / len(slips):+.1f} bps over {len(slips)}")
        if overs:
            bits.append(f"worst stop overshoot {max(overs):+.2f}R over {len(overs)} stops")
        if bits:
            lines.append("Execution: " + " · ".join(bits))
        held = [f"{s} {'LONG' if p['dir'] > 0 else 'SHORT'}" for s, p in (open_positions or {}).items() if p]
        eq = f" · equity ${equity:,.2f}" if equity is not None else ""
        lines.append(f"Open now: {', '.join(held) if held else 'none'}{eq}")
        ctx = (f"Backtest expects ~{BACKTEST['trades_per_week']:g} trades/week, "
               f"{BACKTEST['exp_r']:+.2f}R/trade, PF {BACKTEST['pf']}.")
        if a["n"] < MIN_TRADES_FOR_COMPARISON:
            ctx += f" Only {a['n']} trades so far — far too few to judge (need ~{MIN_TRADES_FOR_COMPARISON}+)."
        lines.append(ctx)
        return "\n".join(lines)
