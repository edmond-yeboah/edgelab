"""Turn the cTrader OAuth redirect `code` into an access token, and list your account IDs.

Run this on YOUR machine (it needs to reach Spotware; the build sandbox can't). Uses only the
Python standard library.

Step A — get the code (browser):
    https://openapi.ctrader.com/apps/auth?client_id=YOUR_CLIENT_ID&redirect_uri=http://localhost/&scope=trading
  Approve, pick your DEMO account, then copy the `code=...` value from the localhost URL.

Step B — exchange it (here):
    python examples/ctrader_get_token.py \
        --client-id YOUR_CLIENT_ID --client-secret YOUR_CLIENT_SECRET \
        --code THE_CODE --redirect-uri http://localhost/

What it does:
  * exchanges the code for an access + refresh token
  * lists your trading accounts with ctidTraderAccountId and isLive (pick the DEMO one)
  * writes credentials to .env (gitignored); it does NOT print the secret or full token

The code expires in a minute or two — if it fails, redo Step A for a fresh code.
"""

from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from pathlib import Path

TOKEN_URL = "https://openapi.ctrader.com/apps/token"
ACCOUNTS_URL = "https://api.spotware.com/connect/tradingaccounts"


def _post_form(url: str, fields: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _get_json(url: str, params: dict) -> dict:
    with urllib.request.urlopen(f"{url}?{urllib.parse.urlencode(params)}", timeout=30) as r:
        return json.loads(r.read().decode())


def _mask(s: str) -> str:
    return f"{s[:4]}…{s[-4:]} (len {len(s)})" if s and len(s) > 8 else "set"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client-id", required=True)
    ap.add_argument("--client-secret", required=True)
    ap.add_argument("--code", required=True, help="the code=... from the localhost redirect")
    ap.add_argument("--redirect-uri", default="http://localhost/")
    ap.add_argument("--token-url", default=TOKEN_URL)
    ap.add_argument("--accounts-url", default=ACCOUNTS_URL)
    ap.add_argument("--env-out", default=".env")
    a = ap.parse_args()

    tok = _post_form(a.token_url, {
        "grant_type": "authorization_code", "code": a.code,
        "redirect_uri": a.redirect_uri, "client_id": a.client_id, "client_secret": a.client_secret,
    })
    if "accessToken" not in tok and "access_token" not in tok:
        raise SystemExit(f"Token exchange failed: {tok}")
    access = tok.get("accessToken") or tok["access_token"]
    refresh = tok.get("refreshToken") or tok.get("refresh_token", "")
    print("access token:", _mask(access))

    print("\nYour trading accounts (pick the DEMO one — isLive should be false):")
    accounts, demo_id = [], None
    try:
        res = _get_json(a.accounts_url, {"access_token": access})
        accounts = res.get("data") or res.get("accounts") or []
        for acc in accounts:
            acc_id = acc.get("ctidTraderAccountId") or acc.get("traderLogin")
            is_live = acc.get("isLive", acc.get("live"))
            print(f"  ctidTraderAccountId={acc_id}  isLive={is_live}  broker={acc.get('brokerName','?')}")
            if is_live in (False, 0, "false") and demo_id is None:
                demo_id = acc_id
    except Exception as e:
        print(f"  (couldn't auto-list accounts: {e}; find the ctidTraderAccountId in the cTrader portal)")

    env = Path(a.env_out)
    lines = {
        "CTRADER_CLIENT_ID": a.client_id,
        "CTRADER_CLIENT_SECRET": a.client_secret,
        "CTRADER_ACCESS_TOKEN": access,
        "CTRADER_REFRESH_TOKEN": refresh,
    }
    if demo_id is not None:
        lines["CTRADER_ACCOUNT_ID"] = str(demo_id)
    env.write_text("".join(f"{k}={v}\n" for k, v in lines.items()))
    print(f"\nWrote {env} (gitignored). "
          + ("Set CTRADER_ACCOUNT_ID is filled with your demo account." if demo_id is not None
             else "Add CTRADER_ACCOUNT_ID=<your demo ctidTraderAccountId> to it."))
    print("Load it before running the bot, e.g.:  set -a && source .env && set +a")
    print("\nShare with your developer only: CLIENT_ID and ACCOUNT_ID. Keep SECRET and token private.")


if __name__ == "__main__":
    main()
