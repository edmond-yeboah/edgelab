"""Re-authentication must self-heal, and side scripts must never rotate the bot's token."""

import pytest

from edgelab.brokers.ctrader import CTraderClient, CTraderConfig, CTraderError


class _Client(CTraderClient):
    def __init__(self, tmp_path, read_only=False, app_error=None, stale_token=False):
        store = str(tmp_path / "rt")
        super().__init__(CTraderConfig("c", "s", "env-token", 1, host_type="demo",
                                       refresh_token="rt0", token_store=store, read_only=read_only))
        self.sent, self.app_error, self.stale_token, self._refreshed = [], app_error, stale_token, False

    def _req(self, name, **kw):
        return (name, kw)

    def _call(self, msg, *a, **k):
        name, kw = msg
        self.sent.append(name)
        if name == "ProtoOAApplicationAuthReq" and self.app_error:
            raise CTraderError(self.app_error)
        if name == "ProtoOARefreshTokenReq":
            self._refreshed = True
            class R: accessToken, refreshToken, expiresIn = "new-access", "rt1", 1
            return R()
        if name == "ProtoOAAccountAuthReq" and self.stale_token and not self._refreshed:
            raise CTraderError("INVALID_REQUEST", "Trading account is not authorized")
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


def test_healthy_token_is_not_refreshed_on_reauth(tmp_path):
    # The fix: a good token must NOT be re-minted on every re-auth — rotating it ripples to a second
    # instance sharing it (read_only) and makes it flap. Refresh only when the token is actually rejected.
    c = _Client(tmp_path)
    c._authenticate()
    assert "ProtoOAAccountAuthReq" in c.sent
    assert "ProtoOARefreshTokenReq" not in c.sent


def test_rejected_token_triggers_one_refresh_then_reauths(tmp_path):
    c = _Client(tmp_path, stale_token=True)
    c._authenticate()
    assert "ProtoOARefreshTokenReq" in c.sent             # refreshed because account auth was rejected
    assert c.cfg.access_token == "new-access"
    assert (tmp_path / "rt").read_text() == "rt1"
    assert (tmp_path / "rt.access").read_text() == "new-access"
    assert c.sent.count("ProtoOAAccountAuthReq") == 2      # failed with old token, then ok after refresh


def test_read_only_session_never_refreshes_and_uses_the_bots_persisted_token(tmp_path):
    (tmp_path / "rt.access").write_text("bots-access")
    c = _Client(tmp_path, read_only=True)
    c._authenticate()
    assert "ProtoOARefreshTokenReq" not in c.sent        # would revoke the running bot's token
    assert c.cfg.access_token == "bots-access"
