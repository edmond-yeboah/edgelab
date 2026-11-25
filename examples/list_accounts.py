"""List the cTrader trading accounts an access token can reach (READ-ONLY: no refresh, no writes).

Use it to find a demo account's ctidTraderAccountId for a --no-refresh second bot that shares the main
bot's token. It reads the access token the main bot already persisted — it never refreshes or rotates
anything, so it is safe to run next to the live bot.

    CTRADER_TOKEN_STORE=/home/ubuntu/edgelab/.ctrader_refresh_token \
        python examples/list_accounts.py

Prints each account's ctidTraderAccountId, login, isLive and broker. Pick the demo one you want and put
its ctidTraderAccountId in the second bot's CTRADER_ACCOUNT_ID. Uses only the standard library.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

ACCOUNTS_URL = "https://api.spotware.com/connect/tradingaccounts"


def main() -> None:
    store = os.environ.get("CTRADER_TOKEN_STORE", ".ctrader_refresh_token")
    access_path = Path(store + ".access")
    token = os.environ.get("CTRADER_ACCESS_TOKEN", "")
    if access_path.exists():
        token = access_path.read_text().strip() or token
    if not token:
        raise SystemExit(
            f"No access token found. Point CTRADER_TOKEN_STORE at the main bot's token store (so I can read "
            f"{access_path}), or set CTRADER_ACCESS_TOKEN.")

    url = f"{ACCOUNTS_URL}?{urllib.parse.urlencode({'access_token': token})}"
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.loads(r.read().decode())
    accounts = data.get("data") or data.get("accounts") or []
    if not accounts:
        raise SystemExit(f"No accounts returned. Raw response: {json.dumps(data)[:400]}")

    print(f"{'ctidTraderAccountId':>22}  {'login':>10}  {'isLive':>6}  broker")
    for a in accounts:
        print(f"{str(a.get('ctidTraderAccountId', '?')):>22}  {str(a.get('traderLogin', '?')):>10}  "
              f"{str(a.get('isLive', a.get('live', '?'))):>6}  {a.get('brokerName', '?')}")
    print("\nFor the Pepperstone EUR+BTC bot, use the ctidTraderAccountId of the row with isLive=False "
          "and the Pepperstone login.")


if __name__ == "__main__":
    main()
