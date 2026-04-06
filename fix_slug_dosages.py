"""
One-off cleanup: normalise dosage labels stored in variant_amounts.

Fixes:
  1. Slug-format hyphens:  "10-mg"           → "10 mg"
  2. Trailing extra text:  "10 mg single vial" → "10 mg"
  3. Bad amount_unit:      "-mg"              → "mg"

Safe to run while the worker is stopped or live — only touches rows that need it.
"""
import json
import re
import sys

from app.db.session import SessionLocal
from app.models.entities import ListingVariant, VendorListing

_SLUG_RE = re.compile(r'(\d)-([a-z])', re.IGNORECASE)
_DOSAGE_TOKEN_RE = re.compile(r'\d+(?:\.\d+)?\s*(?:mg|mcg|ug|g|iu|ml)\b', re.IGNORECASE)


def _normalize_label(label: str) -> str:
    """Normalise a single variant label to canonical '10 mg' form.

    Steps:
      1. Strip slug hyphen between digit and unit: '10-mg' → '10mg'
      2. If the label contains extra text beyond the dosage (e.g. '10 mg single vial'),
         extract just the dosage token(s).
      3. Normalise spacing: '10mg' → '10 mg'.
    """
    # Step 1: strip slug hyphen
    s = _SLUG_RE.sub(r'\1\2', label.strip())

    # Step 2: extract dosage token(s) if extra text is present
    tokens = _DOSAGE_TOKEN_RE.findall(s)
    if len(tokens) == 1:
        s = tokens[0].strip()
    elif len(tokens) > 1:
        # Multiple dosages in one label (e.g. blend "30mg/4mg") — keep all joined
        s = ' '.join(t.strip() for t in tokens)
    # else: no dosage token found — keep the stripped string as-is

    # Step 3: normalise spacing (remove all spaces then reinsert at digit→letter boundary)
    s = re.sub(r'\s+', '', s).lower()
    s = _SLUG_RE.sub(r'\1\2', s)               # re-apply after lowercasing
    s = re.sub(r'(\d)([a-z])', r'\1 \2', s)    # '10mg' → '10 mg'
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
