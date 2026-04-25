"""
One-off cleanup: null out wp_vendor_listings.variant_amounts on listings where
it's clearly stale or wrong, so the renderer falls back to amount_mg.

Targets two patterns observed in the wild:

  1. Blend descriptions   — variant_amounts entry contains '+' or '=', e.g.
     "50MG GHK-Cu + 10MG TB-500 + 10MG BPC-157 = 70MG per vial".
     Splitting these into separate dose chips creates phantom variants.

  2. amount_mg mismatch   — every dose token in variant_amounts is different
     from the listing's amount_mg, e.g. listing.amount_mg=500 with
     variant_amounts=["10 mg","20 mg"] (Orbitrex NAD+ pattern). The JSON is
     left over from a prior crawl on a different page or attribute group.

Skips listings that have wp_listing_variants rows (those are handled by
cleanup_stale_variant_amounts.py and must be kept authoritative).

Idempotent. Safe to run while worker is stopped or live.
"""
import json
import re
import sys

from app.db.session import SessionLocal
from app.models.entities import ListingVariant, VendorListing

_AMOUNT_RE = re.compile(r'(\d+(?:\.\d+)?)\s*(mg|mcg|ug|g|iu|ml)\b', re.IGNORECASE)


def _has_blend_marker(entry: str) -> bool:
    return "+" in entry or "=" in entry


def _amounts_in(entry: str) -> list[float]:
    return [float(m.group(1)) for m in _AMOUNT_RE.finditer(entry)]


def main():
    db = SessionLocal()
    cleared_blend = cleared_mismatch = 0
    try:
        listings = (
            db.query(VendorListing)
            .filter(VendorListing.variant_amounts.isnot(None))
            .all()
        )

        for l in listings:
            # Listings with structured variant rows are handled elsewhere.
            n_variants = (
                db.query(ListingVariant)
                .filter(ListingVariant.listing_id == l.id)
                .count()
            )
            if n_variants > 0:
                continue

            try:
                raw = json.loads(l.variant_amounts)
            except Exception:
                continue
            if not isinstance(raw, list) or not raw:
                continue

            # Pattern 1: any blend-shaped entry → null the whole field
            if any(_has_blend_marker(str(e)) for e in raw):
                print(f"[{l.id}] BLEND  '{l.vendor_product_name}'  was={l.variant_amounts}")
                l.variant_amounts = None
                cleared_blend += 1
                continue

            # Pattern 2: no entry overlaps amount_mg → stale
            if l.amount_mg is not None:
                all_amounts: list[float] = []
                for e in raw:
                    all_amounts.extend(_amounts_in(str(e)))
                if all_amounts and not any(abs(a - l.amount_mg) < 1e-9 for a in all_amounts):
                    print(f"[{l.id}] MISMATCH amount_mg={l.amount_mg}  '{l.vendor_product_name}'  was={l.variant_amounts}")
                    l.variant_amounts = None
                    cleared_mismatch += 1

        if cleared_blend or cleared_mismatch:
            db.commit()
            print(f"\nCleared {cleared_blend} blend-shaped + {cleared_mismatch} mismatched listing(s).")
        else:
            print("Nothing to clean.")
    except Exception as exc:
        db.rollback()
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
