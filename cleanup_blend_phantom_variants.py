"""
One-off cleanup: delete wp_listing_variants rows that disagree with the
listing's locked dose. Targets phantom variants left behind by blend-label
mis-parsing (e.g. "10/3 mg" produced a 3 mg variant row).

Conservative — only acts on listings where:
  - dose_locked = True (admin curated the headline dose)
  - amount_mg is set
  - the variant row's dose is different from amount_mg

Idempotent. Safe to run while worker is stopped or live.
"""
import sys

from app.db.session import SessionLocal
from app.models.entities import ListingVariant, VendorListing


def main():
    db = SessionLocal()
    deleted_total = 0
    try:
        listings = (
            db.query(VendorListing)
            .filter(VendorListing.dose_locked.is_(True))
            .filter(VendorListing.amount_mg.isnot(None))
            .all()
        )

        for l in listings:
            stale = (
                db.query(ListingVariant)
                .filter(ListingVariant.listing_id == l.id)
                .filter(ListingVariant.dosage != l.amount_mg)
                .all()
            )
            if not stale:
                continue
            print(f"[{l.id}] {l.vendor_product_name}  amount_mg={l.amount_mg}")
            for v in stale:
                print(f"    delete variant {v.dosage} {v.unit}  price=${v.price}  in_stock={v.in_stock}")
            db.query(ListingVariant).filter(
                ListingVariant.listing_id == l.id,
                ListingVariant.dosage != l.amount_mg,
            ).delete(synchronize_session=False)
            deleted_total += len(stale)

        if deleted_total:
            db.commit()
            print(f"\nDeleted {deleted_total} stale variant row(s).")
        else:
            print("Nothing to delete.")
    except Exception as exc:
        db.rollback()
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
