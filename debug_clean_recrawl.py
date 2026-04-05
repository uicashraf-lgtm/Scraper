import sys
if sys.platform.startswith("win"):
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from app.db.session import SessionLocal
from app.models.entities import CanonicalProduct, ProductAlias, VendorListing, PriceHistory
from app.workers.runner import crawl_vendor, crawl_listing

db = SessionLocal()

# Wipe all stale data
db.query(PriceHistory).delete()
db.query(VendorListing).delete()
db.query(ProductAlias).delete()
db.query(CanonicalProduct).delete()
db.commit()
print("Cleared all listings, canonical products, aliases, price history")

db.close()

# Re-discover all product URLs
print("Running crawl_vendor(1)...")
crawl_vendor(1)
print("crawl_vendor done")

# Get all new listing IDs
db = SessionLocal()
listings = db.query(VendorListing).filter(VendorListing.vendor_id == 1).all()
ids = [l.id for l in listings]
db.close()
print(f"Discovered {len(ids)} listings, scraping...")

# Scrape each
for i, lid in enumerate(ids):
    print(f"  [{i+1}/{len(ids)}] id={lid}")
    crawl_listing(lid)

# Final check
db = SessionLocal()
ok = db.query(VendorListing).filter(VendorListing.last_price != None).count()
total = db.query(VendorListing).count()
products = db.query(CanonicalProduct).count()
print(f"\nDone: {ok}/{total} listings with prices, {products} canonical products")
db.close()
