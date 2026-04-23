# PeptiPrices Backend (CMS/UI excluded)

Backend stack implementing:
- scraper/worker pipeline
- MySQL storage
- FastAPI API layer
- Redis queue + pubsub
- SSE live price updates

## Features implemented

- Live price updates via SSE (`/api/stream/prices`)
- Vendor management with auto-crawl on vendor add
- Editable monitored URL list (single add, bulk import, toggle)
- Auto-discovery of product URLs from store/category pages for any vendor
- Optional vendor-level scrape config (selectors/pattern/pagination limits) for hard sites
- Block/rate-limit detection with dashboard alerts/logs
- Manual vendor price override endpoint
- Affiliate links via vendor template (`{url}` token)
- Product variation mapping to one canonical product (`BPC-157`, `BPC157`, `bpc 157` normalize to same key)
- Vendor-specific adapters plus generic fallback

## Scraper behavior

Flow:
1. Vendor target URL (store/category/product) is crawled.
2. Crawler discovers product links and follows pagination on same domain (`next`, `?page=`, `/page/N`).
3. If discovery finds URLs, each is queued and scraped.
4. If discovery finds nothing, target URL itself is scraped.
5. Extraction uses:
   - optional vendor selectors (`price_selector`, `price_attr`, `name_selector`)
   - adapter chain (merchant/platform)
   - JSON-LD fallback
   - generic text fallback
6. If blocked/JS-heavy, Playwright fallback is attempted.

Merchant-specific adapters preconfigured:
- genpeptide.com
- ezpeptides.com
- ameanopeptides.com

## Run

1. Copy env file:
```bash
cp .env.example .env
```

2. Start services:
```bash
docker compose up --build
```

3. API docs:
- `http://localhost:8002/docs`

### Production (VPS) — PM2

On the VPS the API is supervised by PM2 via
[`deploy/ecosystem.config.js`](deploy/ecosystem.config.js) so that `pm2 list`
shows it alongside NodeBB:

```bash
pm2 start deploy/ecosystem.config.js
pm2 save
```

Full steps and troubleshooting: see
[`deploy/nginx/README.md`](deploy/nginx/README.md#running-the-api-under-pm2).

## Core endpoints

- `POST /api/admin/vendors`
  - Creates vendor and (optionally) auto-enqueues crawl.
  - Accepts optional `scrape_config` for generic vendor onboarding.
- `PATCH /api/admin/vendors/{vendor_id}/scrape-config`
  - Update selectors/patterns for that vendor.
- `GET /api/admin/vendors/{vendor_id}/scrape-config`
  - Read current scrape config for that vendor.
- `POST /api/admin/vendors/{vendor_id}/selector-test`
  - Test selectors on a URL and return extracted name/price preview.
- `POST /api/admin/vendors/{vendor_id}/targets`
  - Add one monitored URL.
- `POST /api/admin/vendors/{vendor_id}/targets/import`
  - Bulk import monitored URLs.
- `PATCH /api/admin/targets/{target_id}?enabled=true|false`
  - Toggle monitored URL.
- `POST /api/admin/listings/{listing_id}/manual-price`
  - Override crawler price manually.
- `POST /api/admin/products/{product_id}/aliases`
  - Force alias mapping to a canonical product.
- `GET /api/dashboard/crawl-status`
  - Fetch status/error/blocked counts per listing.
- `GET /api/dashboard/alerts`
  - Block/rate-limit alerts.
- `GET /api/products/search?q=BPC`
  - Search canonical products.
- `GET /api/products/{product_id}/prices`
  - Side-by-side vendor prices for one canonical product.
- `GET /api/stream/prices`
  - SSE stream for instant client updates.

## Notes

- DB tables are created automatically on API startup (`Base.metadata.create_all`).
- For production schema migrations, move to Alembic.



