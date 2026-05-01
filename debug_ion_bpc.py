"""
Diagnostic: fetch BPC157+TB500 from ionpeptide.com Store API and trace
exactly what price / variations the scraper sees.

Run on VPS:  python debug_ion_bpc.py
"""
import json
import logging
logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")

BASE_URL = "https://ionpeptide.com"
SLUG = "bpc157tb500"

from app.scraper.rate_limiter import http_get_with_retry
from app.scraper.wc_api import _store_price, process_wc_store_product

# ── 1. Fetch product from Store API ──────────────────────────────────────────
endpoint = f"{BASE_URL}/wp-json/wc/store/v1/products"
print(f"\n[1] GET {endpoint}?slug={SLUG}")
resp = http_get_with_retry(endpoint, params={"slug": SLUG}, timeout=15, max_retries=2)
print(f"    HTTP {resp.status_code}")
if resp.status_code != 200:
    print("    FAILED — trying without v1")
    endpoint2 = f"{BASE_URL}/wp-json/wc/store/products"
    resp = http_get_with_retry(endpoint2, params={"slug": SLUG}, timeout=15, max_retries=2)
    print(f"    HTTP {resp.status_code}")

items = resp.json()
prod = items[0] if isinstance(items, list) and items else None
if not prod:
    print("    No product returned — check slug or endpoint")
    raise SystemExit(1)

# ── 2. Show raw prices and variations from API ────────────────────────────────
print(f"\n[2] Raw API fields for '{prod.get('name')}'")
print(f"    type       : {prod.get('type')}")
prices = prod.get("prices") or {}
print(f"    prices.price       : {prices.get('price')!r}")
print(f"    prices.regular_price: {prices.get('regular_price')!r}")
print(f"    prices.price_range : {json.dumps(prices.get('price_range'))}")
raw_variations = prod.get("variations") or []
print(f"    variations ({len(raw_variations)}): {json.dumps(raw_variations[:3])}{'...' if len(raw_variations) > 3 else ''}")
attrs = prod.get("attributes") or []
print(f"    attributes ({len(attrs)}): {json.dumps(attrs)}")

# ── 3. _store_price result ────────────────────────────────────────────────────
print(f"\n[3] _store_price() → {_store_price(prices)!r}")

# ── 4. Full process_wc_store_product ─────────────────────────────────────────
print(f"\n[4] process_wc_store_product() with base_url='{BASE_URL}'")
try:
    data = process_wc_store_product(prod, base_url=BASE_URL)
    print(f"    price          : {data['price']!r}")
    print(f"    price_max      : {data.get('price_max')!r}")
    print(f"    amount_mg      : {data.get('amount_mg')!r}")
    print(f"    variant_amounts: {data.get('variant_amounts')!r}")
    print(f"    variants       : {data.get('variants')!r}")
except Exception as exc:
    print(f"    CRASHED: {type(exc).__name__}: {exc}")
    import traceback; traceback.print_exc()
