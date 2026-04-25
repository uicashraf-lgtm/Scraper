"""
One-off cleanup: prune wp_vendor_listings.variant_amounts entries that no
longer have a matching wp_listing_variants row, and dedupe case-variants
("10MG" vs "10mg") that the scraper left behind.

For listings with NO listing_variants rows, only dedupe casing — don't
prune, since variant_amounts is the only dose source.

Idempotent. Safe to run while the worker is stopped or live.
"""
import json
import re
import sys

from app.db.session import SessionLocal
from app.models.entities import ListingVariant, VendorListing

_SLUG_RE = re.compile(r'(\d)-([a-z])', re.IGNORECASE)
_DOSAGE_PREFIX_RE = re.compile(r'^(\d+(?:\.\d+)?(?:mg|mcg|ug|g|iu|ml))', re.IGNORECASE)


def _normalize(label: str) -> str:
    s = _SLUG_RE.sub(r'\1\2', str(label).strip())
    s = re.sub(r'\s+', '', s).lower()
    m = _DOSAGE_PREFIX_RE.match(s)
    if m:
        s = m.group(1)
    return re.sub(r'(\d)([a-z])', r'\1 \2', s)


def _variant_label(v: ListingVariant) -> str:
    amt = v.dosage
    unit = (v.unit or "mg").lower()
    return f"{int(amt)} {unit}" if amt == int(amt) else f"{amt} {unit}"


def main():
    db = SessionLocal()
    pruned = deduped = 0
    try:
        listings = (
            db.query(VendorListing)
            .filter(VendorListing.variant_amounts.isnot(None))
            .all()
        )

        for l in listings:
            try:
                raw = json.loads(l.variant_amounts)
            except Exception:
                continue
            if not isinstance(raw, list) or not raw:
                continue

            variants = (
                db.query(ListingVariant)
                .filter(ListingVariant.listing_id == l.id)
                .all()
            )

            if variants:
                allowed = {_normalize(_variant_label(v)) for v in variants}
                seen, kept = set(), []
                for e in raw:
                    n = _normalize(e)
                    if n in allowed and n not in seen:
                        seen.add(n)
                        kept.append(e)
                if kept != raw:
                    l.variant_amounts = json.dumps(kept) if kept else None
                    pruned += 1
                    print(f"  prune listing {l.id}: {raw} -> {kept}")
            else:
                seen, kept = set(), []
                for e in raw:
                    n = _normalize(e)
                    if n not in seen:
                        seen.add(n)
                        kept.append(e)
                if kept != raw:
                    l.variant_amounts = json.dumps(kept)
                    deduped += 1
                    print(f"  dedup listing {l.id}: {raw} -> {kept}")

        if pruned or deduped:
            db.commit()
            print(f"\nDone — pruned {pruned} listing(s), deduped {deduped} listing(s).")
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
