"""
Front-page broken-link audit.

Fetches the configured public front page, extracts every link that looks like a
product / buy / vendor redirector link, and HTTP-checks each (following
redirects). A link is flagged broken if the final response is 4xx/5xx, the
host is unreachable, or the request times out. Results are persisted to
`wp_broken_link_runs` and `wp_broken_link_checks` so admins can review.
"""
import logging
from datetime import datetime
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.entities import BrokenLinkCheck, BrokenLinkRun

logger = logging.getLogger(__name__)


# Path fragments that almost always mean "this is a product or affiliate redirector".
# Used to keep same-domain links that point at the vendor (e.g. /go/123, /buy/abc).
_REDIRECTOR_HINTS = (
    "/go/", "/out/", "/click", "/redirect", "/r/",
    "/buy/", "/affiliate", "/visit/", "/track/",
    "/product/", "/products/", "/item/",
)


def _host(url: str) -> str:
    return (urlparse(url).netloc or "").lower().removeprefix("www.")


def extract_candidate_links(html: str, base_url: str) -> list[str]:
    """Pull product/buy candidate URLs out of the front page HTML.

    Heuristics: any absolute http(s) <a href> that is either external to the
    page's own host, or hits a known same-domain redirector path."""
    soup = BeautifulSoup(html, "html.parser")
    base_host = _host(base_url)

    seen: set[str] = set()
    out: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        href = href.strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue

        url = urljoin(base_url, href).split("#", 1)[0]
        if not url.startswith(("http://", "https://")):
            continue

        host = _host(url)
        if not host:
            continue

        is_external = host != base_host
        is_redirector = any(hint in url.lower() for hint in _REDIRECTOR_HINTS)
        if not (is_external or is_redirector):
            continue

        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


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


def run_broken_link_check(db: Session, frontend_url: str | None = None) -> BrokenLinkRun:
    """Execute one audit run and persist results. Idempotent per URL (upsert)."""
    target = frontend_url or settings.frontend_url
    if not target:
        raise ValueError("frontend_url is not configured")

    run = BrokenLinkRun(frontend_url=target, started_at=datetime.utcnow(), status="running")
    db.add(run)
    db.flush()  # populate run.id

    headers = {"User-Agent": settings.scraper_user_agent}
    timeout = settings.broken_link_request_timeout

    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers=headers,
        ) as client:
            try:
                page_resp = client.get(target)
            except httpx.RequestError as exc:
                run.status = "error"
                run.error = f"front page unreachable: {exc.__class__.__name__}: {exc}"[:1000]
                run.finished_at = datetime.utcnow()
                db.commit()
                logger.error("[broken-links] front page unreachable %s: %s", target, exc)
                return run

            if page_resp.status_code >= 400:
                run.status = "error"
                run.error = f"front page HTTP {page_resp.status_code}"
                run.finished_at = datetime.utcnow()
                db.commit()
                logger.error("[broken-links] front page returned %s for %s",
                             page_resp.status_code, target)
                return run

            urls = extract_candidate_links(page_resp.text, str(page_resp.url))
            if len(urls) > settings.broken_link_max_links:
                logger.warning("[broken-links] truncating %d candidate links to max %d",
                               len(urls), settings.broken_link_max_links)
                urls = urls[: settings.broken_link_max_links]

            run.total_links = len(urls)
            db.flush()
            logger.info("[broken-links] auditing %d link(s) from %s", len(urls), target)

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
