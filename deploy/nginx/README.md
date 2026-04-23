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

```bash
cd /var/www/peptiprices-backend

# One-time: create log dir the ecosystem file points at
sudo mkdir -p /var/log/peptiprices
sudo chown "$USER":"$USER" /var/log/peptiprices

# Start (or restart) the API under PM2
pm2 start deploy/ecosystem.config.js
pm2 save

# Make PM2 come back after reboot (run the command it prints)
pm2 startup
```

Verify it shows up:

```bash
pm2 list
# expected: two entries — `peptiprices-api` and `nodebb`
pm2 logs peptiprices-api --lines 50
curl -i http://127.0.0.1:8002/health
```

If `pm2 list` only shows `nodebb`, the API is being run some other way
(manual `python run.py`, tmux, systemd, Docker). Stop that first, then
run `pm2 start deploy/ecosystem.config.js` so PM2 owns the process.
Adjust `cwd` / `interpreter` in `ecosystem.config.js` if the repo or
virtualenv live at a different path on your VPS.

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
