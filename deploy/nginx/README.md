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

1. **uvicorn running on 127.0.0.1:8002**
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

## Co-hosting NodeBB (optional)

If you want to run NodeBB on the same VPS at e.g. `community.aminoprices.com`,
put it in **its own file** (`/etc/nginx/sites-available/community`) with a
**distinct** `server_name`. Do NOT add `community.aminoprices.com` to the
accupep.conf server blocks, and do NOT add `accupep.com` to the NodeBB
server blocks. Each domain gets exactly one server block. Remember to add
a DNS A record for `community.aminoprices.com` pointing at the VPS IP
(managed at Hostinger, since that's where the aminoprices.com zone lives).
