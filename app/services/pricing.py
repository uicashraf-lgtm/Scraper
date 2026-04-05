from datetime import datetime
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.entities import Alert, CrawlLog, ManualPriceOverride, PriceHistory, VendorListing
from app.services.queue import publish_event


def is_blocked_response(status_code: int | None, body: str | None) -> bool:
    # Delegate to the single authoritative blocking detector in the fetch layer
    from app.scraper.fetch import looks_blocked
    return looks_blocked(status_code, body)


def create_crawl_log(
    db: Session,
    *,
    listing_id: int | None,
    vendor_id: int | None,
    status: str,
    http_status: int | None = None,
    is_blocked: bool = False,
    message: str | None = None,
):
    db.add(
        CrawlLog(
            listing_id=listing_id,
            vendor_id=vendor_id,
            status=status,
            http_status=http_status,
            is_blocked=is_blocked,
            message=message,
        )
    )


def set_manual_price(db: Session, listing_id: int, price: float, currency: str, note: str | None, created_by: str):
    db.query(ManualPriceOverride).filter(
        ManualPriceOverride.listing_id == listing_id,
        ManualPriceOverride.active.is_(True),
    ).update({"active": False})

    override = ManualPriceOverride(
        listing_id=listing_id,
        price=price,
        currency=currency,
        note=note,
        created_by=created_by,
        active=True,
    )
    db.add(override)
    db.add(PriceHistory(listing_id=listing_id, source="manual", price=price, currency=currency))

    listing = db.query(VendorListing).filter(VendorListing.id == listing_id).first()
    payload = {
        "type": "price_update",
        "listing_id": listing_id,
        "vendor_id": listing.vendor_id if listing else None,
        "price": price,
        "currency": currency,
        "source": "manual",
        "timestamp": datetime.utcnow().isoformat(),
    }
    publish_event(payload)


def maybe_raise_block_alert(db: Session, listing: VendorListing):
    if listing.blocked_count >= settings.block_alert_threshold:
        db.add(
            Alert(
                vendor_id=listing.vendor_id,
                severity="critical",
                message=f"Vendor listing {listing.id} hit blocked threshold ({listing.blocked_count}).",
            )
        )
