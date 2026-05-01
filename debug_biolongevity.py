"""
Diagnostic: fetch all products from biolongevitylabs.com Store API and show
exactly which products get a price vs. which get filtered out (price=None).

Run on VPS:  python debug_biolongevity.py
"""
import logging
logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

BASE_URL = "https://biolongevitylabs.com"

from app.scraper.wc_api import fetch_wc_store_products, _store_price, process_wc_store_product

print(f"Fetching products from {BASE_URL} ...\n")
products = fetch_wc_store_products(BASE_URL)
print(f"Store API returned {len(products)} products\n")

kept = []
filtered = []

for p in products:
    name = p.get("name", "?")
    ptype = p.get("type", "?")
    prices_obj = p.get("prices") or {}
    raw_price = prices_obj.get("price")
    raw_regular = prices_obj.get("regular_price")
    price_range = prices_obj.get("price_range")
    computed = _store_price(prices_obj)

    if computed is None:
        filtered.append((name, ptype, raw_price, raw_regular, price_range))
    else:
        kept.append((name, ptype, computed))

print(f"=== KEPT ({len(kept)}) ===")
for name, ptype, price in kept:
    print(f"  [{ptype:25s}] ${price:8.2f}  {name}")

print(f"\n=== FILTERED — price=None ({len(filtered)}) ===")
for name, ptype, raw_price, raw_regular, price_range in filtered:
    print(f"  [{ptype:25s}] price={raw_price!r:10s} regular={raw_regular!r:10s} range={price_range}  {name}")
