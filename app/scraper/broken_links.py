"""
Broken-link audit over stored vendor listings.

Iterates every enabled vendor's listing URLs (the same URLs the WordPress
plugin renders as "buy" links on the front page) and HTTP-checks each.
A link is flagged broken if the final response is 4xx/5xx, the host is
unreachable, or the request times out. Results land in
`wp_broken_link_runs` and `wp_broken_link_checks` so admins can review.
"""
import logging
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.entities import BrokenLinkCheck, BrokenLinkRun, Vendor, VendorListing

logger = logging.getLogger(__name__)

# Sentinel stored in BrokenLinkRun.frontend_url so the (NOT NULL) column is
# populated and old front-page rows remain distinguishable from new DB-sourced
# runs.
_SOURCE_LABEL = "db:vendor_listings"


def _check_link(client: httpx.Client, url: str) -> tuple[int | None, str | None, str | None]:
    """Return (status_code, final_url, error). Tries HEAD first, falls back to
    GET when the server rejects HEAD. Follows redirects."""
    try:
        resp = client.head(url)
        if resp.status_code in (403, 405, 501) or resp.status_code >= 500:
            # Some sites block HEAD or return misleading codes for it.
            resp = client.get(url)
        return resp.status_code, str(resp.url), None
    except httpx.TimeoutException:
        return None, None, "timeout"
    except httpx.TooManyRedirects:
        return None, None, "too_many_redirects"
    except httpx.RequestError as exc:
        return None, None, f"network: {exc.__class__.__name__}"
    except Exception as exc:  # defensive: never let a single URL crash the run
        return None, None, f"error: {exc.__class__.__name__}"


def _is_broken(status_code: int | None, error: str | None) -> bool:
    if error:
        return True
    if status_code is None:
        return True
    return status_code >= 400


def _upsert_check(
    db: Session,
    run_id: int,
    url: str,
    status_code: int | None,
    final_url: str | None,
    error: str | None,
) -> None:
    row = db.query(BrokenLinkCheck).filter(BrokenLinkCheck.url == url).first()
    if row is None:
        row = BrokenLinkCheck(url=url)
        db.add(row)
    row.run_id = run_id
    row.status_code = status_code
    row.final_url = final_url
    row.error = error[:255] if error else None
    row.is_broken = _is_broken(status_code, error)
    row.checked_at = datetime.utcnow()


def _collect_listing_urls(db: Session) -> list[str]:
    """Pull every URL we'd put on the front page: enabled vendors only,
    skipping admin-entered manual listings (they're not vendor pages) and
    listings without an http(s) URL."""
    rows = (
        db.query(VendorListing.url)
        .join(Vendor, Vendor.id == VendorListing.vendor_id)
        .filter(Vendor.enabled.is_(True))
        .filter(VendorListing.is_manual.is_(False))
        .all()
    )
    seen: set[str] = set()
    urls: list[str] = []
    for (url,) in rows:
        if not url or not isinstance(url, str):
            continue
        if not url.startswith(("http://", "https://")):
            continue
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def run_broken_link_check(db: Session) -> BrokenLinkRun:
    """Execute one audit run over every stored vendor listing URL and persist
    results. Idempotent per URL (upsert)."""
    run = BrokenLinkRun(
        frontend_url=_SOURCE_LABEL,
        started_at=datetime.utcnow(),
        status="running",
    )
    db.add(run)
    db.flush()  # populate run.id

    urls = _collect_listing_urls(db)
    if len(urls) > settings.broken_link_max_links:
        logger.warning("[broken-links] truncating %d listing URLs to max %d",
                       len(urls), settings.broken_link_max_links)
        urls = urls[: settings.broken_link_max_links]

    run.total_links = len(urls)
    db.flush()
    logger.info("[broken-links] auditing %d listing URL(s)", len(urls))

    headers = {"User-Agent": settings.scraper_user_agent}
    timeout = settings.broken_link_request_timeout

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
            broken_count = 0
            for url in urls:
                status_code, final_url, error = _check_link(client, url)
                if _is_broken(status_code, error):
                    broken_count += 1
                    logger.info("[broken-links] BROKEN %s status=%s err=%s",
                                url, status_code, error)
                _upsert_check(
                    db,
                    run_id=run.id,
                    url=url,
                    status_code=status_code,
                    final_url=final_url,
                    error=error,
                )
                db.flush()

        run.broken_count = broken_count
        run.status = "done"
        run.finished_at = datetime.utcnow()
        db.commit()
        logger.info("[broken-links] DONE %d broken / %d total", broken_count, len(urls))
        return run
    except Exception as exc:
        run.status = "error"
        run.error = f"{exc.__class__.__name__}: {exc}"[:1000]
        run.finished_at = datetime.utcnow()
        db.commit()
        logger.exception("[broken-links] run failed")
        raise
