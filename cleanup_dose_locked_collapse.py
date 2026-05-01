"""
One-off cleanup: unlock listings whose dose_locked=True is hiding multiple
real ListingVariant rows. Caused by the patch_listing variant-label path
incorrectly setting dose_locked when admin edits a single variant on a
multi-variant listing.

Safe to run while worker is stopped or live. Idempotent.
"""
import sys

from app.db.session import SessionLocal
from app.models.entities import ListingVariant, VendorListing


def main():
    db = SessionLocal()
    try:
        locked = db.query(VendorListing).filter(VendorListing.dose_locked.is_(True)).all()
        affected = []
        for l in locked:
            n_variants = (
                db.query(ListingVariant)
                .filter(ListingVariant.listing_id == l.id)
                .count()
            )
            if n_variants > 1:
                affected.append((l, n_variants))

        if not affected:
            print("Nothing to unlock.")
            return

        print(f"Found {len(affected)} listing(s) locked to one dose despite having multiple variant rows:\n")
        for l, n in affected:
            print(f"  [{l.id}] {l.vendor_product_name}  amount_mg={l.amount_mg}  variants={n}")

        for l, _ in affected:
            l.dose_locked = False
        db.commit()
        print(f"\nUnlocked {len(affected)} listing(s).")
    except Exception as exc:
        db.rollback()
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
