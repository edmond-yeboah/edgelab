from edgelab.live.notify import LoggingTransport, Notifier, TelegramTransport, from_env


def test_entry_exit_flip_and_killswitch_messages():
    t = LoggingTransport()
    n = Notifier(t)
    n("bar", {"ts": "t0", "equity": 500, "position": None})                       # flat, no msg
    n("bar", {"ts": "t1", "equity": 500, "position": {"dir": 1, "qty": 1000}})    # entry
    n("bar", {"ts": "t2", "equity": 505, "position": {"dir": 1, "qty": 1000}})    # hold, no msg
    n("bar", {"ts": "t3", "equity": 510, "position": {"dir": -1, "qty": 1000}})   # flip
    n("bar", {"ts": "t4", "equity": 508, "position": None})                       # exit
    n("kill_switch", {"ts": "t5", "reason": "max drawdown"})
    joined = " | ".join(t.sent)
    assert "ENTRY LONG" in joined
    assert "FLIP to SHORT" in joined
    assert "EXIT" in joined
    assert "KILL SWITCH" in joined
    assert len(t.sent) == 4   # hold bars and the initial flat bar send nothing


def test_heartbeat_throttling():
    t = LoggingTransport()
    n = Notifier(t, heartbeat_every=3)
    for i in range(6):
        n("bar", {"ts": f"t{i}", "equity": 500, "position": None})
    # only bars 3 and 6 produce a heartbeat
    assert len(t.sent) == 2 and all("equity" in m for m in t.sent)


def test_telegram_transport_uses_injected_opener_and_never_raises():
    captured = {}
    def opener(url, data):
        captured["url"], captured["data"] = url, data
    tr = TelegramTransport("BOTTOKEN", "CHAT", opener=opener)
    tr.send("hello")
    assert "api.telegram.org/botBOTTOKEN/sendMessage" in captured["url"]
    assert b"hello" in captured["data"] and b"CHAT" in captured["data"]


def test_from_env_returns_none_without_config():
    assert from_env({}) is None
    assert from_env({"TELEGRAM_BOT_TOKEN": "x"}) is None
    assert from_env({"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_CHAT_ID": "y"}) is not None


def test_exit_message_includes_profit_and_reason():
    t = LoggingTransport()
    n = Notifier(t)
    n("bar", {"ts": "t1", "equity": 500, "position": {"dir": 1, "qty": 1000}})       # entry
    n("bar", {"ts": "t2", "equity": 530, "position": None,
              "closed": [{"pnl": 30.0, "reason": "signal_exit", "dir": 1}]})          # exit, +30
    joined = " | ".join(t.sent)
    assert "EXIT" in joined and "signal_exit" in joined
    assert "P&L +30.00" in joined and "🟢" in joined


def test_exit_message_shows_a_loss():
    t = LoggingTransport()
    n = Notifier(t)
    n("bar", {"ts": "t1", "equity": 500, "position": {"dir": -1, "qty": 1000}})
    n("bar", {"ts": "t2", "equity": 480, "position": None,
              "closed": [{"pnl": -20.0, "reason": "stop", "dir": -1}]})
    joined = " | ".join(t.sent)
    assert "EXIT (stop)" in joined and "P&L -20.00" in joined and "🔴" in joined


def test_started_recovered_and_error_messages():
    t = LoggingTransport()
    n = Notifier(t)
    n("started", {"host": "demo", "equity": 101000, "symbols": ["EURUSD", "BTCUSD"]})
    n("recovered", {"ts": "t1"})
    n("error", {"detail": "cycle error: boom"})
    joined = " | ".join(t.sent)
    assert "edgelab started" in joined and "EURUSD" in joined
    assert "Reconnected" in joined
    assert "Error: cycle error: boom" in joined


def test_started_seeds_open_positions_so_restart_does_not_reannounce_entries():
    t = LoggingTransport()
    n = Notifier(t)
    # Restart with an already-open (adopted) EURUSD short
    n("started", {"host": "demo", "equity": 100000, "symbols": ["EURUSD"],
                  "positions": {"EURUSD": {"dir": -1, "qty": 85000.0}}})
    # First bar after restart shows that same position — must NOT be announced as an ENTRY
    n("bar", {"symbol": "EURUSD", "ts": "t1", "equity": 100000, "position": {"dir": -1, "qty": 85000.0}})
    joined = " | ".join(t.sent)
    assert "Resuming 1 open: EURUSD SHORT" in joined
    assert "ENTRY" not in joined
    # A genuine change (the exit) still alerts
    n("bar", {"symbol": "EURUSD", "ts": "t2", "equity": 100100, "position": None,
              "closed": [{"pnl": 100.0, "reason": "signal_exit", "dir": -1}]})
    assert "EXIT" in " | ".join(t.sent)


def test_rejected_order_alerts_with_reason_then_throttles_per_symbol():
    t = LoggingTransport()
    n = Notifier(t)
    n("bar", {"symbol": "XAUUSD", "ts": "t1", "equity": 100000, "position": None,
              "rejected": [{"symbol": "XAUUSD", "side": "SELL", "reason": "TRADING_BAD_VOLUME"}]})
    assert any("XAUUSD" in m and "rejected" in m and "TRADING_BAD_VOLUME" in m for m in t.sent)
    n0 = len(t.sent)
    n("bar", {"symbol": "XAUUSD", "ts": "t2", "equity": 100000, "position": None,
              "rejected": [{"symbol": "XAUUSD", "side": "SELL", "reason": "TRADING_BAD_VOLUME"}]})
    assert len(t.sent) == n0                      # same symbol again -> throttled, no new alert
    n("bar", {"symbol": "BTCUSD", "ts": "t3", "equity": 100000, "position": None,
              "rejected": [{"symbol": "BTCUSD", "side": "SELL", "reason": "NOT_ENOUGH_MONEY"}]})
    assert any("BTCUSD" in m and "NOT_ENOUGH_MONEY" in m for m in t.sent)   # different symbol still alerts


def test_market_open_and_close_messages():
    t = LoggingTransport()
    n = Notifier(t)
    n("market_closed", {"symbol": "EURUSD", "ts": "t1"})
    n("market_open", {"symbol": "EURUSD", "ts": "t2"})
    joined = " | ".join(t.sent)
    assert "EURUSD market closed" in joined
    assert "EURUSD market open" in joined


def test_daily_account_summary_is_throttled_to_once_a_day():
    t = LoggingTransport()
    n = Notifier(t)
    n("account", {"ts": "t1", "equity": 101000, "kill_switch": False})   # first -> sends
    n("account", {"ts": "t2", "equity": 101050, "kill_switch": False})   # same day -> silent
    assert len(t.sent) == 1 and "Daily status" in t.sent[0]
    n._last_daily -= 86401                                               # a day passes
    n("account", {"ts": "t3", "equity": 101100, "kill_switch": False})
    assert len(t.sent) == 2
