# VPS deployment — nginx

This directory documents the nginx reverse-proxy config for the Scraper API.

## Topology

```
                                  ┌───────────────────────────┐
  Hostinger (managed WP) ────────▶│ https://accupep.com       │
  aminoprices.com WP plugin       │   VPS, nginx :443         │
  calls /api/... server-side      │   └─▶ uvicorn 127.0.0.1   │
                                  │         :8002 (run.py)    │
                                  └───────────────────────────┘

  Browser also calls                accupep.com/api/stream/prices
  (SSE) directly from the           directly from the page JS.
  WordPress theme JS.
```

**Important:** `aminoprices.com` is **not** hosted on the VPS. It is on
Hostinger's managed WordPress (LiteSpeed + Cloudflare). The VPS only serves
`accupep.com`, which is the public API host.

## Install

On the VPS:

```bash
# 1. Copy the config into place
sudo cp deploy/nginx/accupep.conf /etc/nginx/sites-available/accupep

# 2. Make sure nothing else is claiming accupep.com
sudo grep -rl "accupep.com" /etc/nginx/sites-enabled/ | xargs -r sudo rm

# 3. Enable the new config
sudo ln -sf /etc/nginx/sites-available/accupep /etc/nginx/sites-enabled/accupep

# 4. Test and reload
sudo nginx -t && sudo systemctl reload nginx
```

## Preconditions

1. **uvicorn running on 127.0.0.1:8002** (managed by PM2 — see
   [Running the API under PM2](#running-the-api-under-pm2) below)
   ```bash
   curl -i http://127.0.0.1:8002/health   # expect {"ok": true}
   ```
2. **Let's Encrypt cert present** at `/etc/letsencrypt/live/accupep.com/`.
   If missing:
   ```bash
   sudo certbot certonly --nginx -d accupep.com -d www.accupep.com
   ```
3. **DNS A record** `accupep.com` → VPS public IP.
4. **Firewall** allows inbound 80/tcp and 443/tcp:
   ```bash
   sudo ufw allow 80/tcp && sudo ufw allow 443/tcp
   ```

## Verification

```bash
# From the VPS itself
curl -i https://accupep.com/health

# From anywhere
curl -i https://accupep.com/api/products
```

Both should return 200. The WordPress plugin `/wp-json/pa/v1/products` on
aminoprices.com should then stop returning 502.

## Running the API under PM2

The API (`run.py` → uvicorn on :8002, plus the scraper worker it supervises)
should run under PM2 so that it restarts on crash/reboot and so that
`pm2 list` shows it alongside NodeBB.

Ecosystem file: [`deploy/ecosystem.config.js`](../ecosystem.config.js).
It auto-detects the repo root (`cwd`) from its own location and picks
a Python interpreter in this order: `$PEPTI_PYTHON` → `./.venv/bin/python`
→ `python3` on `$PATH`.

### Start

From inside the repo checkout (wherever it lives on the VPS):

```bash
cd /path/to/Scraper         # the directory containing run.py

# If you use a virtualenv, create .venv here first (python3 -m venv .venv
# && .venv/bin/pip install -r requirements.txt). Otherwise the ecosystem
# falls back to system python3 — which must have the deps installed.

pm2 start deploy/ecosystem.config.js
pm2 save
pm2 startup                  # run the command it prints, for reboot persistence
```

### Verify

```bash
pm2 list
# expected: `peptiprices-api` AND `nodebb`
pm2 logs peptiprices-api --lines 80
curl -i http://127.0.0.1:8002/health     # expect {"ok": true}
```

### Troubleshooting: `pm2 list` still shows only `nodebb`

Work through these in order — one of them is almost certainly the cause:

1. **Wrong user.** PM2 is per-user. Check which user owns NodeBB's PM2
   daemon and run the API start command as **that same user**:
   ```bash
   ps -o user= -p "$(pgrep -f 'PM2.*God Daemon' | head -1)"
   ```
   If NodeBB is under `root` but you ran `pm2 start` as a different user,
   you have two separate PM2 daemons and neither sees the other's apps.

2. **`pm2 start` errored silently.** Re-run it and read the output:
   ```bash
   pm2 start deploy/ecosystem.config.js
   pm2 jlist | python3 -c 'import json,sys;[print(a["name"], a["pm2_env"]["status"]) for a in json.load(sys.stdin)]'
   ```
   If the app is listed but `status=errored`, look at the error log
   path it prints (`pm2 describe peptiprices-api`) and `pm2 logs
   peptiprices-api --err --lines 100`.

3. **Interpreter not found.** If `.venv/bin/python` doesn't exist and
   `python3` isn't on PATH for PM2's environment, the process fork
   fails. Fix by pointing at the right binary explicitly:
   ```bash
   PEPTI_PYTHON=/usr/bin/python3 pm2 start deploy/ecosystem.config.js --update-env
   ```

4. **Another process is already on :8002.** PM2 will keep restarting and
   crashing. Find and stop it:
   ```bash
   sudo lsof -iTCP:8002 -sTCP:LISTEN
   ```
   Common culprits: a stray `python run.py`, tmux session, systemd unit,
   or `docker compose up`. Stop that first, then `pm2 restart peptiprices-api`.

5. **Ran `pm2 start` from the wrong directory.** You must pass
   `deploy/ecosystem.config.js` as a **path** (not just the filename)
   or `cd` to a directory where that path resolves. The ecosystem
   auto-derives `cwd` from its own location, so the repo can live
   anywhere, but PM2 has to find the file itself.

6. **Dashboard cached.** If you use `pm2 monit` / the PM2 web dashboard,
   `pm2 list` on the CLI is authoritative — trust the CLI output.

Once it shows up, `pm2 save` is required to persist across reboots.

## Co-hosting NodeBB (optional)

Reference config: `community.conf`

The golden rule: **one domain = one nginx file = one server_name**. Never
mix accupep.com and community.aminoprices.com in the same server block.

### Setup steps (do this AFTER accupep.com is verified working)

1. **DNS** — In Hostinger's DNS panel for aminoprices.com, add an A record:
   `community` → `187.124.84.241` (VPS public IP).
   Verify: `dig +short community.aminoprices.com` returns the VPS IP.

2. **NodeBB setup** — On the VPS:
   ```bash
   cd /var/www/aminoprices-forum
   ./nodebb setup
   # URL: http://community.aminoprices.com
   # Port: 4567
   # Database: mongo (accept defaults)
   # Create admin user when prompted
   ```
   Wait for "NodeBB Setup Completed."

3. **Start NodeBB via PM2:**
   ```bash
   cd /var/www/aminoprices-forum
   ./nodebb start
   # Or via PM2:
   pm2 start /var/www/aminoprices-forum/nodebb -- start
   pm2 save
   ```
   Verify: `curl -i http://127.0.0.1:4567` returns HTML.

4. **Install the nginx config:**
   ```bash
   sudo cp deploy/nginx/community.conf /etc/nginx/sites-available/community
   sudo ln -sf /etc/nginx/sites-available/community /etc/nginx/sites-enabled/community
   sudo nginx -t && sudo systemctl reload nginx
   ```

5. **Get SSL cert** (after HTTP is verified working):
   ```bash
   sudo certbot --nginx -d community.aminoprices.com
   ```
   Choose "redirect" when asked. Certbot adds the 443 block automatically.

6. **Verify:** `https://community.aminoprices.com` shows the NodeBB welcome page.
