"""Re-authentication must self-heal, and side scripts must never rotate the bot's token."""

import pytest

from edgelab.brokers.ctrader import CTraderClient, CTraderConfig, CTraderError


class _Client(CTraderClient):
    def __init__(self, tmp_path, read_only=False, app_error=None):
        store = str(tmp_path / "rt")
        super().__init__(CTraderConfig("c", "s", "env-token", 1, host_type="demo",
                                       refresh_token="rt0", token_store=store, read_only=read_only))
        self.sent, self.app_error = [], app_error

    def _req(self, name, **kw):
        return (name, kw)

    def _call(self, msg, *a, **k):
        name, kw = msg
        self.sent.append(name)
        if name == "ProtoOAApplicationAuthReq" and self.app_error:
            raise CTraderError(self.app_error)
        if name == "ProtoOARefreshTokenReq":
            class R: accessToken, refreshToken, expiresIn = "new-access", "rt1", 1
            return R()
        return object()


def test_already_logged_in_app_auth_does_not_block_account_reauth(tmp_path):
    c = _Client(tmp_path, app_error="ALREADY_LOGGED_IN: Open API application is already authorized")
    c._authenticate()
    assert "ProtoOAAccountAuthReq" in c.sent             # got past app auth
    assert c.sent.count("ProtoOAApplicationAuthReq") == 1  # and didn't pointlessly retry it


def test_other_app_auth_errors_still_raise(tmp_path):
    c = _Client(tmp_path, app_error="CH_CLIENT_AUTH_FAILURE: bad secret")
    with pytest.raises(CTraderError):
        c._authenticate()


def test_bot_session_refreshes_and_persists_both_tokens(tmp_path):
    c = _Client(tmp_path)
    c._authenticate()
    assert "ProtoOARefreshTokenReq" in c.sent
    assert (tmp_path / "rt").read_text() == "rt1"
    assert (tmp_path / "rt.access").read_text() == "new-access"


def test_read_only_session_never_refreshes_and_uses_the_bots_persisted_token(tmp_path):
    (tmp_path / "rt.access").write_text("bots-access")
    c = _Client(tmp_path, read_only=True)
    c._authenticate()
    assert "ProtoOARefreshTokenReq" not in c.sent        # would revoke the running bot's token
    assert c.cfg.access_token == "bots-access"
