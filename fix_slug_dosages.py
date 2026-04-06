"""
One-off cleanup: normalise slug-format dosage labels stored in variant_amounts.
e.g. ["10-mg", "5-mg"] → ["10 mg", "5 mg"]

Also fixes amount_unit values like "-mg" on vendor_listings rows.

Safe to run while the worker is stopped or live — only touches rows that need it.
"""
import json
import re
import sys

from app.db.session import SessionLocal
from app.models.entities import ListingVariant, VendorListing

_SLUG_RE = re.compile(r'(\d)-([a-z])', re.IGNORECASE)


def _normalize_label(label: str) -> str:
    """'10-mg' → '10 mg', '5-MG' → '5 mg', '10mg' → '10 mg'"""
    s = re.sub(r'\s+', '', label).lower()
    s = _SLUG_RE.sub(r'\1\2', s)                  # strip hyphen: "10-mg" → "10mg"
    s = re.sub(r'(\d)([a-z])', r'\1 \2', s)       # add space:   "10mg"  → "10 mg"
    return s


def fix_variant_amounts(db) -> int:
    rows = db.query(VendorListing).filter(VendorListing.variant_amounts.isnot(None)).all()
    updated = 0
    for listing in rows:
        try:
            raw = json.loads(listing.variant_amounts)
        except Exception:
            continue
        if not isinstance(raw, list):
            continue
        normalized = [_normalize_label(str(v)) for v in raw]
        if normalized != raw:
            listing.variant_amounts = json.dumps(normalized)
            updated += 1
            print(f"  listing_id={listing.id}  {raw}  →  {normalized}")
    return updated


def fix_amount_unit(db) -> int:
    rows = (
        db.query(VendorListing)
        .filter(VendorListing.amount_unit.isnot(None))
        .all()
    )
    updated = 0
    for listing in rows:
        unit = listing.amount_unit or ""
        fixed = _SLUG_RE.sub(r'\1\2', unit).lower()
        if fixed != unit.lower():
            listing.amount_unit = fixed
            updated += 1
            print(f"  listing_id={listing.id}  amount_unit '{unit}' → '{fixed}'")
    return updated


def fix_listing_variant_units(db) -> int:
    rows = db.query(ListingVariant).all()
    updated = 0
    for lv in rows:
        unit = lv.unit or "mg"
        fixed = _SLUG_RE.sub(r'\1\2', unit).lower()
        if fixed != unit.lower():
            lv.unit = fixed
            updated += 1
            print(f"  listing_variant_id={lv.id}  unit '{unit}' → '{fixed}'")
    return updated


def main():
    db = SessionLocal()
    try:
        print("=== Fixing variant_amounts ===")
        n1 = fix_variant_amounts(db)
        print(f"  → {n1} listing(s) updated")

        print("\n=== Fixing amount_unit ===")
        n2 = fix_amount_unit(db)
        print(f"  → {n2} listing(s) updated")

        print("\n=== Fixing listing_variant units ===")
        n3 = fix_listing_variant_units(db)
        print(f"  → {n3} variant(s) updated")

        total = n1 + n2 + n3
        if total:
            db.commit()
            print(f"\nCommitted {total} change(s).")
        else:
            print("\nNothing to fix — all clean.")
    except Exception as exc:
        db.rollback()
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
