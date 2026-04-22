"""
Trustpilot review scraper.

Given a business domain (e.g. "example.com") or any Trustpilot review URL,
returns the overall star rating and total number of reviews.

Extraction order:
  1. JSON-LD `aggregateRating` (server-rendered, most stable).
  2. `__NEXT_DATA__` hydration blob (Trustpilot is Next.js).
  3. Visible-text regex fallback.
"""

import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from app.scraper.fetch import fetch_page, looks_blocked

logger = logging.getLogger(__name__)

TRUSTPILOT_BASE = "https://www.trustpilot.com/review/"


@dataclass
class TrustpilotResult:
    ok: bool
    url: str
    domain: str | None = None
    business_name: str | None = None
    rating: float | None = None
    review_count: int | None = None
    status_code: int | None = None
    error: str | None = None


def _to_float(val) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _to_int(val) -> int | None:
    if val is None:
        return None
    if isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    try:
        return int(str(val).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _normalize_to_review_url(target: str) -> tuple[str, str]:
    """Return (review_url, domain) for a raw user input."""
    target = target.strip()
    if target.startswith(("http://", "https://")):
        parsed = urlparse(target)
        if "trustpilot.com" in parsed.netloc:
            tail = parsed.path.rstrip("/").rsplit("/", 1)[-1]
            return target, tail.lower()
        domain = parsed.netloc
    else:
        domain = target.split("/", 1)[0]
    domain = domain.lower().removeprefix("www.")
    return f"{TRUSTPILOT_BASE}{domain}", domain


def _extract_from_jsonld(soup: BeautifulSoup) -> tuple[float | None, int | None, str | None]:
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue

        nodes: list = data if isinstance(data, list) else [data]
        expanded: list = []
        for n in nodes:
            if isinstance(n, dict) and isinstance(n.get("@graph"), list):
                expanded.extend(n["@graph"])
            else:
                expanded.append(n)

        for node in expanded:
            if not isinstance(node, dict):
                continue
            agg = node.get("aggregateRating")
            if not isinstance(agg, dict):
                continue
            rating = _to_float(agg.get("ratingValue"))
            count = _to_int(agg.get("reviewCount") or agg.get("ratingCount"))
            name = node.get("name") if isinstance(node.get("name"), str) else None
            if rating is not None or count is not None:
                return rating, count, name
    return None, None, None


def _extract_from_next_data(soup: BeautifulSoup) -> tuple[float | None, int | None, str | None]:
    node = soup.find("script", {"id": "__NEXT_DATA__"})
    if not node:
        return None, None, None
    try:
        data = json.loads(node.string or node.get_text() or "")
    except json.JSONDecodeError:
        return None, None, None

    rating: float | None = None
    count: int | None = None
    name: str | None = None

    stack = [data]
    while stack:
        curr = stack.pop()
        if isinstance(curr, dict):
            if rating is None and "trustScore" in curr:
                rating = _to_float(curr.get("trustScore"))
            if rating is None and "stars" in curr:
                rating = _to_float(curr.get("stars"))
            if count is None and "numberOfReviews" in curr:
                val = curr["numberOfReviews"]
                count = _to_int(val.get("total") if isinstance(val, dict) else val)
            if name is None and isinstance(curr.get("displayName"), str):
                name = curr["displayName"]
            stack.extend(curr.values())
        elif isinstance(curr, list):
            stack.extend(curr)

    return rating, count, name


_RATING_RE = re.compile(
    r"(?:TrustScore|Rated)\s*([0-9]+(?:[.,][0-9]+)?)\s*(?:out\s+)?of\s*5",
    re.IGNORECASE,
)
_COUNT_RE = re.compile(r"([\d,]+)\s+(?:total\s+)?reviews?\b", re.IGNORECASE)


def _extract_from_regex(html: str) -> tuple[float | None, int | None]:
    rating = None
    count = None
    m = _RATING_RE.search(html)
    if m:
        rating = _to_float(m.group(1).replace(",", "."))
    m = _COUNT_RE.search(html)
    if m:
        count = _to_int(m.group(1))
    return rating, count


def scrape_trustpilot(target: str) -> TrustpilotResult:
    url, domain = _normalize_to_review_url(target)
    logger.info("Scraping Trustpilot: %s", url)

    status_code, html, error = fetch_page(url)
    if not html:
        return TrustpilotResult(
            ok=False, url=url, domain=domain,
            status_code=status_code, error=error or "no_html",
        )

    soup = BeautifulSoup(html, "html.parser")

    rating, count, name = _extract_from_jsonld(soup)

    if rating is None or count is None or name is None:
        r2, c2, n2 = _extract_from_next_data(soup)
        rating = rating if rating is not None else r2
        count = count if count is not None else c2
        name = name or n2

    if rating is None or count is None:
        r3, c3 = _extract_from_regex(html)
        rating = rating if rating is not None else r3
        count = count if count is not None else c3

    ok = rating is not None and count is not None

    error_msg: str | None = None
    if not ok:
        # Extraction failed — decide whether the page was blocked or just
        # didn't contain a rating (e.g. domain has no Trustpilot profile)
        if looks_blocked(status_code, html):
            error_msg = f"blocked_http_{status_code}"
        else:
            error_msg = "trustpilot_data_not_found"

    return TrustpilotResult(
        ok=ok,
        url=url,
        domain=domain,
        business_name=name,
        rating=rating,
        review_count=count,
        status_code=status_code,
        error=error_msg,
    )
