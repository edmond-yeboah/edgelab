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
