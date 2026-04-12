"""
Interactive CLI to edit peptide dosages for a specific vendor.

Usage:
    python edit_dose.py                         # lists all vendors, then prompts
    python edit_dose.py "Molecular Edge"        # jumps straight to that vendor
    python edit_dose.py --vendor-id 5           # by vendor ID
"""
import json
import sys

from app.db.session import SessionLocal
from app.models.entities import ListingVariant, Vendor, VendorListing


def find_vendor(db, search: str | None = None, vendor_id: int | None = None):
    """Find vendor by ID or name search. Returns Vendor or None."""
    if vendor_id:
        return db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if search:
        vendors = (
            db.query(Vendor)
            .filter(Vendor.name.ilike(f"%{search}%"))
            .all()
        )
        if len(vendors) == 1:
            return vendors[0]
        if len(vendors) > 1:
            print(f"\nMultiple vendors match '{search}':")
            for v in vendors:
                print(f"  [{v.id}] {v.name}")
            choice = input("\nEnter vendor ID: ").strip()
            return db.query(Vendor).filter(Vendor.id == int(choice)).first()
        print(f"No vendor found matching '{search}'.")
    return None


def list_vendors(db):
    """Print all vendors and let the user pick one."""
    vendors = db.query(Vendor).order_by(Vendor.name).all()
    if not vendors:
        print("No vendors in database.")
        return None
    print("\n=== All Vendors ===")
    for v in vendors:
        status = "enabled" if v.enabled else "disabled"
        print(f"  [{v.id:>3}] {v.name:<40} ({status})")
    choice = input("\nEnter vendor ID: ").strip()
    return db.query(Vendor).filter(Vendor.id == int(choice)).first()


def show_listings(listings: list[VendorListing]):
    """Print listings table."""
    print(f"\n{'#':<4} {'ID':<6} {'Product Name':<45} {'Dose':<10} {'Unit':<6} {'Locked':<7} {'Price':<10}")
    print("-" * 92)
    for i, l in enumerate(listings, 1):
        name = (l.vendor_product_name or "")[:43]
        dose = l.amount_mg if l.amount_mg is not None else "-"
        unit = l.amount_unit or "mg"
        locked = "Yes" if l.dose_locked else "No"
        price = f"${l.last_price:.2f}" if l.last_price else "-"
        print(f"{i:<4} {l.id:<6} {name:<45} {dose:<10} {unit:<6} {locked:<7} {price:<10}")


def edit_listing_dose(db, listing: VendorListing):
    """Edit dose for a single listing."""
    print(f"\n  Current: amount_mg={listing.amount_mg}, amount_unit={listing.amount_unit or 'mg'}, "
          f"dose_locked={listing.dose_locked}")

    new_dose = input("  New dose value (e.g. 5, 10.5) or press Enter to skip: ").strip()
    if not new_dose:
        return False

    new_dose = float(new_dose)
    new_unit = input(f"  Unit [{listing.amount_unit or 'mg'}]: ").strip()
    if not new_unit:
        new_unit = listing.amount_unit or "mg"

    listing.amount_mg = new_dose
    listing.amount_unit = new_unit.lower()
    listing.dose_locked = True

    # Recompute price_per_mg
    if listing.last_price and new_dose > 0:
        listing.price_per_mg = listing.last_price / new_dose

    # Update variant_amounts to reflect override
    lbl = f"{int(new_dose)} {new_unit}" if new_dose == int(new_dose) else f"{new_dose} {new_unit}"
    listing.variant_amounts = json.dumps([lbl])

    print(f"  -> Updated: amount_mg={new_dose}, amount_unit={new_unit}, "
          f"price_per_mg={listing.price_per_mg}, dose_locked=True")
    return True


def edit_all_listings(db, listings: list[VendorListing]):
    """Bulk-set the same dose for all listings."""
    new_dose = input("\nNew dose for ALL listings (e.g. 5, 10.5): ").strip()
    if not new_dose:
        print("Cancelled.")
        return 0

    new_dose = float(new_dose)
    new_unit = input("Unit [mg]: ").strip() or "mg"
    new_unit = new_unit.lower()
    lbl = f"{int(new_dose)} {new_unit}" if new_dose == int(new_dose) else f"{new_dose} {new_unit}"

    count = 0
    for l in listings:
        l.amount_mg = new_dose
        l.amount_unit = new_unit
        l.dose_locked = True
        if l.last_price and new_dose > 0:
            l.price_per_mg = l.last_price / new_dose
        l.variant_amounts = json.dumps([lbl])
        count += 1

    print(f"  -> Updated {count} listing(s) to {lbl}, dose_locked=True")
    return count


def main():
    db = SessionLocal()
    try:
        # Parse args
        search = None
        vendor_id = None
        if len(sys.argv) > 1:
            if sys.argv[1] == "--vendor-id" and len(sys.argv) > 2:
                vendor_id = int(sys.argv[2])
            else:
                search = " ".join(sys.argv[1:])

        # Find vendor
        vendor = find_vendor(db, search=search, vendor_id=vendor_id)
        if not vendor:
            vendor = list_vendors(db)
        if not vendor:
            print("No vendor selected. Exiting.")
            return

        print(f"\n=== Vendor: {vendor.name} (ID {vendor.id}) ===")

        # Get listings
        listings = (
            db.query(VendorListing)
            .filter(VendorListing.vendor_id == vendor.id)
            .order_by(VendorListing.vendor_product_name)
            .all()
        )
        if not listings:
            print("No listings found for this vendor.")
            return

        show_listings(listings)

        print(f"\nOptions:")
        print(f"  Enter a row # (1-{len(listings)}) to edit one listing")
        print(f"  Enter 'all' to set the same dose for all listings")
        print(f"  Enter 'q' to quit")

        changed = 0
        while True:
            choice = input("\n> ").strip().lower()
            if choice in ("q", "quit", "exit"):
                break
            elif choice == "all":
                changed += edit_all_listings(db, listings)
                show_listings(listings)
            elif choice.isdigit():
                idx = int(choice)
                if 1 <= idx <= len(listings):
                    listing = listings[idx - 1]
                    print(f"\n  Editing: {listing.vendor_product_name} (listing ID {listing.id})")
                    if edit_listing_dose(db, listing):
                        changed += 1
                else:
                    print(f"  Invalid row #. Enter 1-{len(listings)}.")
            else:
                print("  Invalid input. Enter a row #, 'all', or 'q'.")

        if changed:
            confirm = input(f"\nCommit {changed} change(s) to database? [y/N]: ").strip().lower()
            if confirm == "y":
                db.commit()
                print(f"Done — {changed} listing(s) updated.")
            else:
                db.rollback()
                print("Rolled back — no changes saved.")
        else:
            print("No changes made.")

    except KeyboardInterrupt:
        db.rollback()
        print("\nAborted.")
    except Exception as exc:
        db.rollback()
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
