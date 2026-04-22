"""
Periodic Trustpilot rating refresh.

Iterates enabled vendors whose Trustpilot data is stale (older than
`trustpilot_refresh_hours`) and updates `rating` / `review_count` from
Trustpilot. Runs inside the scheduler daemon thread.

Failure policy: if the scrape fails (no Trustpilot page, blocked, etc.)
we still advance `trustpilot_checked_at` so we don't hammer the vendor
on every poll, but we leave the existing `rating` / `review_count`
untouched so the plugin keeps showing the last good value.
"""
import logging
import threading
from datetime import datetime, timedelta
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.entities import Vendor
from app.scraper.trustpilot import scrape_trustpilot

logger = logging.getLogger(__name__)


def _domain_from_base_url(base_url: str | None) -> str | None:
    if not base_url:
        return None
    parsed = urlparse(base_url if "://" in base_url else f"https://{base_url}")
    host = parsed.netloc or parsed.path
    host = host.split("/", 1)[0].lower().removeprefix("www.")
    return host or None


def refresh_due_vendors(db: Session, stop_event: threading.Event | None = None) -> int:
    """Refresh Trustpilot data for every enabled vendor whose row is stale.
    Returns the number of vendors actually scraped this cycle.
    If a stop_event is supplied and becomes set between vendors, exits early
    so the caller (systemd) can shut down cleanly without killing Playwright
    mid-fetch."""
    cutoff = datetime.utcnow() - timedelta(hours=settings.trustpilot_refresh_hours)

    vendors = (
        db.query(Vendor)
        .filter(Vendor.enabled.is_(True))
        .filter((Vendor.trustpilot_checked_at.is_(None)) | (Vendor.trustpilot_checked_at < cutoff))
        .all()
    )
    if not vendors:
        return 0

    scraped = 0
    for vendor in vendors:
        if stop_event is not None and stop_event.is_set():
            logger.info("Trustpilot refresh aborted (stop event set) after %d vendor(s).", scraped)
            break
        domain = _domain_from_base_url(vendor.base_url)
        if not domain:
            logger.info("Trustpilot: skipping vendor id=%d (no base_url)", vendor.id)
            continue

        try:
            result = scrape_trustpilot(domain)
        except Exception as exc:
            logger.error("Trustpilot scrape crashed for vendor id=%d (%s): %s",
                         vendor.id, domain, exc)
            vendor.trustpilot_checked_at = datetime.utcnow()
            db.commit()
            continue

        if result.ok and result.rating is not None and result.review_count is not None:
            vendor.rating = result.rating
            vendor.review_count = result.review_count
            logger.info("Trustpilot: %s -> %.1f (%d reviews)",
                        domain, result.rating, result.review_count)
        else:
            logger.info("Trustpilot: %s -> no data (%s); keeping previous rating",
                        domain, result.error)

        vendor.trustpilot_checked_at = datetime.utcnow()
        db.commit()
        scraped += 1

    if scraped:
        logger.info("Trustpilot refresh cycle: %d vendor(s) updated", scraped)
    return scraped
