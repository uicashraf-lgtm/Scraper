"""
One-off cleanup: remove every Retatrutide ("reta") listing for the
Molecular Edge Peptides vendor.

Matches by vendor name (flexible) and any listing whose vendor product
name, canonical product name/alias, or URL contains "reta". Drops the
related ListingVariant, PriceHistory, ManualPriceOverride, CrawlLog, and
VendorTargetURL rows so the next crawl doesn't re-discover the URLs.

Canonical products are left alone — other vendors may still carry
retatrutide.

Usage:
    python remove_reta_molecular_edge.py            # dry run
    python remove_reta_molecular_edge.py --apply    # commit deletes
"""
import sys

from sqlalchemy import or_

from app.db.session import SessionLocal
from app.models.entities import (
    CanonicalProduct,
    CrawlLog,
    ListingVariant,
    ManualPriceOverride,
    PriceHistory,
    Vendor,
    VendorListing,
    VendorTargetURL,
)

VENDOR_NAME_LIKE = "%molecular%edge%"
RETA_LIKE = "%reta%"


def main():
    apply = "--apply" in sys.argv

    db = SessionLocal()
    try:
        vendor = (
            db.query(Vendor)
            .filter(Vendor.name.ilike(VENDOR_NAME_LIKE))
            .order_by(Vendor.id)
            .first()
        )
        if not vendor:
            print(f"No vendor matched '{VENDOR_NAME_LIKE}'. Aborting.")
            sys.exit(1)
        print(f"Vendor: id={vendor.id} name={vendor.name!r}")

        # Listings matching reta on vendor name OR URL OR canonical product name/alias.
        listings = (
            db.query(VendorListing)
            .outerjoin(
                CanonicalProduct,
                CanonicalProduct.id == VendorListing.canonical_product_id,
            )
            .filter(
                VendorListing.vendor_id == vendor.id,
                or_(
                    VendorListing.vendor_product_name.ilike(RETA_LIKE),
                    VendorListing.url.ilike(RETA_LIKE),
                    CanonicalProduct.name.ilike(RETA_LIKE),
                    CanonicalProduct.alias.ilike(RETA_LIKE),
                ),
            )
            .all()
        )

        if not listings:
            print("No reta listings found. Nothing to do.")
            return

        listing_ids = [l.id for l in listings]
        print(f"\nFound {len(listings)} listing(s):")
        for l in listings:
            print(
                f"  id={l.id} name={(l.vendor_product_name or '')!r} "
                f"amount={l.amount_mg}{l.amount_unit or ''} "
                f"price={l.last_price} url={l.url}"
            )

        # Target URLs that would re-discover these listings on the next crawl.
        urls = {l.url for l in listings}
        target_urls = (
            db.query(VendorTargetURL)
            .filter(
                VendorTargetURL.vendor_id == vendor.id,
                VendorTargetURL.url.in_(urls),
            )
            .all()
        )

        variant_count = (
            db.query(ListingVariant)
            .filter(ListingVariant.listing_id.in_(listing_ids))
            .count()
        )
        ph_count = (
            db.query(PriceHistory)
            .filter(PriceHistory.listing_id.in_(listing_ids))
            .count()
        )
        mpo_count = (
            db.query(ManualPriceOverride)
            .filter(ManualPriceOverride.listing_id.in_(listing_ids))
            .count()
        )
        log_count = (
            db.query(CrawlLog)
            .filter(CrawlLog.listing_id.in_(listing_ids))
            .count()
        )

        print(
            f"\nWill delete: {len(listings)} listing(s), "
            f"{variant_count} variant(s), {ph_count} price-history row(s), "
            f"{mpo_count} manual override(s), {log_count} crawl log(s), "
            f"{len(target_urls)} target URL(s)."
        )

        if not apply:
            print("\nDry run — re-run with --apply to commit.")
            return

        db.query(ListingVariant).filter(
            ListingVariant.listing_id.in_(listing_ids)
        ).delete(synchronize_session=False)
        db.query(PriceHistory).filter(
            PriceHistory.listing_id.in_(listing_ids)
        ).delete(synchronize_session=False)
        db.query(ManualPriceOverride).filter(
            ManualPriceOverride.listing_id.in_(listing_ids)
        ).delete(synchronize_session=False)
        db.query(CrawlLog).filter(
            CrawlLog.listing_id.in_(listing_ids)
        ).delete(synchronize_session=False)
        db.query(VendorListing).filter(
            VendorListing.id.in_(listing_ids)
        ).delete(synchronize_session=False)
        if target_urls:
            db.query(VendorTargetURL).filter(
                VendorTargetURL.id.in_([t.id for t in target_urls])
            ).delete(synchronize_session=False)

        db.commit()
        print("Done.")
    except Exception as exc:
        db.rollback()
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
