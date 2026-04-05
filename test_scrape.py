"""
Quick diagnostic: scrape a single URL and print exactly what happened.
Usage:  python test_scrape.py <url>
"""
import sys
import logging

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")

url = sys.argv[1] if len(sys.argv) > 1 else input("Enter URL to test: ").strip()

from app.scraper.fetch import fetch_page, looks_blocked, scrape_url

print(f"\n{'='*60}")
print(f"Testing: {url}")
print('='*60)

# Step 1: raw fetch
status_code, html, error = fetch_page(url)
print(f"\n[fetch_page]")
print(f"  status_code : {status_code}")
print(f"  html_len    : {len(html) if html else 0}")
print(f"  error       : {error}")
if html:
    print(f"  looks_blocked(status, html): {looks_blocked(status_code, html)}")
    print(f"  first 500 chars of html:")
    print("  " + html[:500].replace("\n", " "))

# Step 2: full scrape
print(f"\n[scrape_url]")
result = scrape_url(url)
print(f"  ok           : {result.ok}")
print(f"  status_code  : {result.status_code}")
print(f"  adapter      : {result.adapter}")
print(f"  product_name : {result.product_name}")
print(f"  price        : {result.price}")
print(f"  currency     : {result.currency}")
print(f"  message      : {result.message}")
print(f"  in_stock     : {result.in_stock}")
print(f"  amount_mg    : {result.amount_mg}")
print(f"  price_per_mg : {result.price_per_mg}")
if result.body_excerpt:
    print(f"  body_excerpt (first 300):")
    print("  " + result.body_excerpt[:300].replace("\n", " "))
