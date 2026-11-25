"""run_basket.build_cfg: env/arg validation and the --no-refresh (shared-token) wiring, no network."""

import argparse
import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "run_basket", Path(__file__).resolve().parent.parent / "examples" / "run_basket.py")
run_basket = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_basket)


def _args(no_refresh=False):
    return argparse.Namespace(no_refresh=no_refresh)


def _env(**over):
    e = {"CTRADER_CLIENT_ID": "c", "CTRADER_CLIENT_SECRET": "s", "CTRADER_ACCOUNT_ID": "5378281"}
    e.update(over)
    return e


def test_normal_mode_requires_an_access_token():
    with pytest.raises(SystemExit):
        run_basket.build_cfg(_args(no_refresh=False), _env())                # no CTRADER_ACCESS_TOKEN
    cfg = run_basket.build_cfg(_args(no_refresh=False), _env(CTRADER_ACCESS_TOKEN="tok"))
    assert cfg.read_only is False and cfg.access_token == "tok" and cfg.account_id == 5378281


def test_no_refresh_skips_the_access_token_requirement_and_sets_read_only(tmp_path):
    store = str(tmp_path / "rt")
    (tmp_path / "rt.access").write_text("shared-access")                     # the main bot's token exists
    cfg = run_basket.build_cfg(_args(no_refresh=True), _env(CTRADER_TOKEN_STORE=store))
    assert cfg.read_only is True                                            # won't refresh / rotate the token
    assert cfg.token_store == store and cfg.account_id == 5378281
    # access token is read from the shared store at auth time, not required in env here


def test_no_refresh_errors_clearly_if_the_shared_token_is_missing(tmp_path):
    store = str(tmp_path / "rt")                                            # no .access file written
    with pytest.raises(SystemExit) as e:
        run_basket.build_cfg(_args(no_refresh=True), _env(CTRADER_TOKEN_STORE=store))
    assert ".access" in str(e.value)


def test_missing_core_vars_still_fail_in_no_refresh_mode(tmp_path):
    store = str(tmp_path / "rt")
    (tmp_path / "rt.access").write_text("shared-access")
    bad = {"CTRADER_CLIENT_ID": "c", "CTRADER_TOKEN_STORE": store}          # no secret / account id
    with pytest.raises(SystemExit):
        run_basket.build_cfg(_args(no_refresh=True), bad)
