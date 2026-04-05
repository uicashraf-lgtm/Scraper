import logging
import re
from collections import deque
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.models.entities import Vendor
from app.scraper.fetch import fetch_page, looks_blocked

logger = logging.getLogger(__name__)


EXCLUDE_PATTERNS = [
    r"/cart",
    r"/checkout",
    r"/account",
    r"/login",
    r"/register",
    r"/privacy",
    r"/terms",
]

INCLUDE_HINTS = ["product", "shop", "store", "peptide", "item"]
PAGINATION_HINTS = ["page", "paged", "next"]


def _normalize_domain(url: str) -> str:
    return (urlparse(url).netloc or "").lower().replace("www.", "")


def _same_site(url: str, base: str) -> bool:
    return _normalize_domain(url) == _normalize_domain(base)


def _looks_excluded(url: str) -> bool:
    lower = url.lower()
    return any(re.search(p, lower) for p in EXCLUDE_PATTERNS)


def _extract_pagination_links(soup: BeautifulSoup, page_url: str, base_url: str) -> list[str]:
    out: list[str] = []

    # Common rel=next link element
    rel_next = soup.find("link", rel=lambda v: v and "next" in str(v).lower())
    if rel_next and rel_next.get("href"):
        out.append(urljoin(page_url, str(rel_next.get("href"))))

    for a in soup.select("a[href]"):
        href = a.get("href")
        if not href or not isinstance(href, str):
            continue
        candidate = urljoin(page_url, href.strip())
        if not candidate.startswith("http"):
            continue
        if not _same_site(candidate, base_url):
            continue

        txt = a.get_text(" ", strip=True).lower()
        lower = candidate.lower()
        if txt in {"next", "next page", ">", ">>", "older"}:
            out.append(candidate)
            continue
        if any(h in lower for h in ["?page=", "&page=", "/page/"]) or any(h in txt for h in PAGINATION_HINTS):
            out.append(candidate)

    # De-dup preserve order
    seen = set()
    unique = []
    for url in out:
        if url in seen:
            continue
        seen.add(url)
        unique.append(url)
    return unique


def _extract_product_links(soup: BeautifulSoup, page_url: str, vendor: Vendor, max_urls: int) -> list[str]:
    selector = vendor.product_link_selector or "a[href]"
    nodes = soup.select(selector)
    pattern = re.compile(vendor.product_link_pattern, re.I) if vendor.product_link_pattern else None

    out: list[str] = []
    for node in nodes:
        href = node.get("href")
        if not href or not isinstance(href, str):
            continue

        url = urljoin(page_url, href.strip())
        if not url.startswith("http"):
            continue
        if not _same_site(url, vendor.base_url):
            continue
        if _looks_excluded(url):
            continue

        if pattern:
            if not pattern.search(url):
                continue
        else:
            lower = url.lower()
            if not any(h in lower for h in INCLUDE_HINTS):
                continue

        if url not in out:
            out.append(url)
        if len(out) >= max_urls:
            break

    return out


def discover_product_urls(seed_url: str, vendor: Vendor, hints=None) -> list[str]:
    max_urls = vendor.max_discovered_urls or 120
    max_pages = vendor.max_discovery_pages or 8

    logger.info("[discovery] START vendor='%s' seed=%s max_pages=%d max_urls=%d cookies=%s",
                vendor.name, seed_url, max_pages, max_urls,
                len(hints.cookies) if hints and hints.cookies else 0)

    queue = deque([seed_url])
    visited_pages: set[str] = set()
    discovered: list[str] = []

    while queue and len(visited_pages) < max_pages and len(discovered) < max_urls:
        page = queue.popleft()
        if page in visited_pages:
            continue
        visited_pages.add(page)

        logger.info("[discovery] Fetching page %d/%d: %s", len(visited_pages), max_pages, page)
        status_code, html, error = fetch_page(page, hints=hints)

        if not html or (status_code and status_code >= 400):
            logger.warning("[discovery] Skipping page (status=%s error=%s): %s", status_code, error, page)
            continue
        if looks_blocked(status_code, html):
            logger.warning("[discovery] Page appears blocked (status=%s): %s", status_code, page)
            continue

        soup = BeautifulSoup(html, "html.parser")

        # Detect login wall (page loaded as 200 but redirected to login form)
        if soup.find("form", {"action": lambda v: v and ("login" in v.lower() or "my-account" in v.lower())}):
            logger.warning("[discovery] Page looks like a login wall (no auth?): %s", page)

        new_links = _extract_product_links(soup, page, vendor, max_urls=max_urls)
        logger.info("[discovery] Found %d product links on page %s", len(new_links), page)

        for url in new_links:
            if url not in discovered:
                discovered.append(url)
            if len(discovered) >= max_urls:
                break

        if len(discovered) >= max_urls:
            break

        pagination = _extract_pagination_links(soup, page, vendor.base_url)
        for next_page in pagination:
            if next_page not in visited_pages:
                queue.append(next_page)

    logger.info("[discovery] DONE vendor='%s' — %d product URLs found across %d pages",
                vendor.name, len(discovered), len(visited_pages))
    return discovered
