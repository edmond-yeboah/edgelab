# Running edgelab 24/7 on an Oracle Cloud Always Free VM

Goal: the basket bot runs continuously on a free, always-on VM — independent of your Mac — and
restarts itself on crash, refreshes its own token, and logs everything.

Do this when you have time and daylight: the Oracle **account** step can take minutes to a day to verify.

---

## 1. Create the Oracle Cloud account (you — browser)
1. Go to cloud.oracle.com → "Start for free". Sign up (email, phone, and a card for identity
   verification — Always Free resources are not charged).
2. Pick a home **region** near you (e.g. one in Europe/Middle East for lower latency to Ghana).
3. Wait for the account to be provisioned (sometimes a few minutes, sometimes hours).

## 2. Create an Always Free VM
1. Console → **Compute → Instances → Create instance**.
2. **Image & shape:** Ubuntu 22.04. For shape choose an **Always Free-eligible** one:
   - `VM.Standard.A1.Flex` (Ampere ARM) — pick 1 OCPU / 6 GB (well within free limits), or
   - `VM.Standard.E2.1.Micro` (AMD) if ARM capacity is unavailable in your region.
   - If you see "out of capacity" for ARM, try again later or another availability domain — this is
     common on the free ARM shape.
3. **SSH keys:** let it generate a key pair and **download the private key** (or paste your own public
   key). You'll use this to log in.
4. Create. Note the instance's **public IP**.

Networking: outbound is open by default (the bot only needs outbound TCP 443 + 5035 to cTrader — no
inbound rules to add). SSH (port 22) inbound is allowed by the default security list.

## 3. Get the code onto the VM
From your Mac, either **scp** the project up:
```bash
scp -i /path/to/private_key -r ~/Documents/projects/personal/edgelab ubuntu@<PUBLIC_IP>:~/edgelab
```
…or (cleaner long-term) push edgelab to a private GitHub repo and `git clone` it on the VM.

## 4. Install and configure (on the VM, over SSH)
```bash
ssh -i /path/to/private_key ubuntu@<PUBLIC_IP>

sudo apt update && sudo apt install -y python3-venv python3-pip
cd ~/edgelab
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[ctrader,data]"

# credentials — create ~/edgelab/.env  (chmod 600 it; never commit it)
cat > ~/edgelab/.env <<'EOF'
CTRADER_CLIENT_ID=...
CTRADER_CLIENT_SECRET=...
CTRADER_ACCESS_TOKEN=...
CTRADER_REFRESH_TOKEN=...
CTRADER_ACCOUNT_ID=49414241
CTRADER_TOKEN_STORE=/home/ubuntu/edgelab/.ctrader_refresh_token
# optional alerts:
# TELEGRAM_BOT_TOKEN=...
# TELEGRAM_CHAT_ID=...
EOF
chmod 600 ~/edgelab/.env
```
**Before this**, regenerate the client secret + tokens (the old ones were shown in chat) and use the
fresh values here. The bot now auto-refreshes the access token daily, so it won't die at the 30-day mark.

## 5. Run it under systemd (auto-restart + logs)
Create the service:
```bash
sudo tee /etc/systemd/system/edgelab.service >/dev/null <<'EOF'
[Unit]
Description=edgelab trend basket bot
After=network-online.target
Wants=network-online.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/edgelab
EnvironmentFile=/home/ubuntu/edgelab/.env
ExecStart=/home/ubuntu/edgelab/.venv/bin/python examples/run_basket.py --live --timeframe H4
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now edgelab
```

## 6. Watch it
```bash
systemctl status edgelab           # running?
journalctl -u edgelab -f           # live log (bar events, orders, account line)
```
Stop / start / restart:
```bash
sudo systemctl stop edgelab
sudo systemctl restart edgelab
```

## 7. First-run checklist
- [ ] Regenerated secret + tokens; `.env` has the fresh ones and is `chmod 600`.
- [ ] `journalctl` shows "cTrader connected + authed" then per-bar events every 4h.
- [ ] Open positions appear in the cTrader app with an **SL** attached.
- [ ] After ~24h, the log shows "access token refreshed".
- [ ] (Optional) Telegram pings on entries/exits/kill-switch.

## Notes
- The bot is tiny (one connection, a check every 4h), so the smallest free shape is plenty.
- Start on **demo** (`host_type` is demo-only in `run_basket.py`). Never point this at live money until a
  long demo run matches the backtest.
- Account-level kill switch (`--account-max-dd`, default 20%) flattens everything if total equity drops
  too far. The per-position server-side stops protect you even if the VM reboots.
