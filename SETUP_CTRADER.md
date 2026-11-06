# Connecting edgelab to a cTrader demo (Pepperstone or FP Markets)

This gets the bot trading on a **free demo account** with virtual money. No deposit, no risk.
Do every step on **demo** and leave live trading off until the strategy has passed validation.

## 1. Open a demo account with a cTrader broker
1. Create a broker demo account that supports cTrader:
   - **Pepperstone:** sign up, choose the **cTrader** platform, open a **Demo** account.
   - **FP Markets:** same idea — pick cTrader, open a Demo account.
2. Note the demo account's login. You'll link it to the Open API below.

## 2. Create a cTID and register an Open API application
1. Go to the cTrader **Open API** portal (connect.spotware.com) and sign in / create a **cTID**.
2. Create an **Application**. You'll receive a **Client ID** and **Client Secret**.
3. Set the redirect URI to something you control (e.g. `http://localhost/` is fine for manual token capture).

## 3. Authorize your demo account and get an access token (OAuth)
1. Open the authorization URL for your app with scope `trading`, e.g.
   `https://connect.spotware.com/apps/auth?client_id=<CLIENT_ID>&redirect_uri=<REDIRECT>&scope=trading`
2. Approve, and select your **demo** trading account.
3. Exchange the returned `code` for an **access token** (and refresh token) at the token endpoint.
   The helper package `ctrader-oauth-fetcher` automates this for simple cases.
4. Find your **account ID** (the `ctidTraderAccountId`) for the demo account — the token response
   / account list returns it. This is the numeric id edgelab needs, not the broker login.

## 4. Install and configure
```bash
pip install -e ".[ctrader]"        # installs ctrader-open-api + twisted

export CTRADER_CLIENT_ID=...
export CTRADER_CLIENT_SECRET=...
export CTRADER_ACCESS_TOKEN=...
export CTRADER_ACCOUNT_ID=...      # the numeric ctidTraderAccountId of the DEMO account
```
Keep these in a `.env` you never commit (it's already in `.gitignore`). Treat the client secret
and access token like passwords.

## 5. Finish the one integration point
`src/edgelab/brokers/ctrader.py` is a reviewed draft. The network methods raise
`NotImplementedError` with the exact Open API message to send documented inline:
`connect` (app auth → account auth → symbols list), `get_trendbars`
(`ProtoOAGetTrendbarsReq`), `new_market_order` (`ProtoOANewOrderReq` **with `relativeStopLoss`**),
`close_position`, and `reconcile_positions`. Implement them with the OpenApiPy SDK, verifying
message names against help.ctrader.com/open-api. I can do this with you — it's best done against
the live demo endpoint where we can see responses.

## 6. Run it
```bash
# offline dry-run first (no network): replay history through the full live loop
python examples/run_demo.py --replay data/eurusd_h1.parquet --symbol EURUSD

# then the real demo
python examples/run_demo.py --ctrader --symbol EURUSD --timeframe H1
```

## Safety gates that are already enforced
- **Demo by default.** The broker's `host_type` is `demo`; live needs `--host live` **and**
  `EDGELAB_ALLOW_LIVE=I_UNDERSTAND_THE_RISK`.
- **Account/mode match.** On startup edgelab checks the authenticated account's `isLive` flag
  against the selected host and refuses on any mismatch (so a misplaced token can't trade live).
- **Server-side stop.** The protective stop is attached to the order; a disconnect can't strip it.
- **Reconnect-safe.** Open positions are reconciled from the venue by label instead of blind resubmits.

## Measure real costs
Once connected, record the demo's actual spreads and commissions for your pair and put them in
`config/default.yaml` under `costs.fx`, replacing the placeholders. Backtests are only as honest
as those numbers.

## When you later consider live (Ghana)
- Forex: no local retail licence yet in Ghana; use the Tier-1-regulated entity your broker assigns
  and confirm the current position before funding.
- Crypto (if you switch): legal since the Dec 2025 VASP Act, but exchanges serving Ghana must be
  BoG-licensed — check your venue's status before real money.
- Only fund an account after the strategy clears the acceptance gates out-of-sample and on demo.
