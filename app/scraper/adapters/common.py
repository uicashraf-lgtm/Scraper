import json
from typing import Any

from bs4 import BeautifulSoup

from app.scraper.adapters.base import AdapterResult


def parse_jsonld_candidates(soup: BeautifulSoup) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.text
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except Exception:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
        elif isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict):
                    out.append(item)
    return out


def extract_category_from_html(soup: BeautifulSoup) -> str | None:
    """Extract product category from WooCommerce breadcrumbs or common HTML patterns."""
    # WooCommerce breadcrumb: last link before the product name
    for sel in [
        "nav.woocommerce-breadcrumb a",
        ".breadcrumb a",
        "[itemtype*='BreadcrumbList'] a",
    ]:
        links = soup.select(sel)
        # Skip first (Home) and last (product itself), take the deepest category
        cat_links = [a for a in links if a.get_text(strip=True).lower() not in ("home", "shop", "products", "all")]
        if cat_links:
            return cat_links[-1].get_text(strip=True)

    # posted_in (WooCommerce category meta)
    posted = soup.select_one(".posted_in a")
    if posted:
        return posted.get_text(strip=True)
    return None


def extract_tags_from_html(soup: BeautifulSoup) -> list[str]:
    """Extract product tags from WooCommerce tag links or common HTML patterns."""
    tags: list[str] = []
    # WooCommerce: .tagged_as contains tag links
    for a in soup.select(".tagged_as a, .product_tags a, [rel='tag']"):
        txt = a.get_text(strip=True)
        if txt and txt.lower() not in ("home", "shop"):
            tags.append(txt)
    return list(dict.fromkeys(tags))  # deduplicate, preserve order


def extract_category_from_jsonld(candidates: list[dict[str, Any]]) -> str | None:
    """Extract category from JSON-LD Product data."""
    for item in candidates:
        cat = item.get("category")
        if isinstance(cat, str) and cat.strip():
            return cat.strip()
        # Sometimes category is nested: {"@type": "Thing", "name": "..."}
        if isinstance(cat, dict) and cat.get("name"):
            return cat["name"]
        # BreadcrumbList
        if item.get("@type") == "BreadcrumbList":
            elements = item.get("itemListElement") or []
            # Skip first (Home) and last (product), take deepest category
            cat_elements = [e for e in elements
                           if e.get("name", "").lower() not in ("home", "shop", "products")]
            if cat_elements:
                return cat_elements[-1].get("name")
    return None


def extract_from_jsonld(soup: BeautifulSoup) -> AdapterResult:
    candidates = parse_jsonld_candidates(soup)
    category = extract_category_from_jsonld(candidates)
    for item in candidates:
        offers = item.get("offers") if isinstance(item.get("offers"), dict) else None
        if not offers:
            continue
        price = offers.get("price")
        if price is None:
            continue
        try:
            value = float(str(price).replace(",", ""))
        except ValueError:
            continue
        return AdapterResult(
            ok=True,
            product_name=item.get("name"),
            price=value,
            currency=offers.get("priceCurrency", "USD"),
            message=None,
            category=category,
        )
    return AdapterResult(ok=False, product_name=None, price=None, currency=None, message="jsonld_price_not_found")


def read_text(soup: BeautifulSoup, selectors: list[str]) -> str | None:
    for sel in selectors:
        node = soup.select_one(sel)
        if node:
            txt = node.get_text(" ", strip=True)
            if txt:
                return txt
    return None


def read_attr(soup: BeautifulSoup, selectors: list[str], attr: str) -> str | None:
    for sel in selectors:
        node = soup.select_one(sel)
        if node and node.has_attr(attr):
            raw = node.get(attr)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    return None


def parse_price_from_text(text: str) -> tuple[float | None, str | None]:
    import re

    m = re.search(r"([^\d\s]?)(\d+(?:[\.,]\d{1,2})?)", text)
    if not m:
        return None, None
    symbol, raw = m.groups()
    try:
        value = float(raw.replace(",", ""))
    except ValueError:
        return None, None
    currency = {"$": "USD", "EUR": "EUR", "GBP": "GBP"}.get(symbol, "USD")
    return value, currency


def title_name(soup: BeautifulSoup) -> str | None:
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    h1 = soup.select_one("h1")
    if h1:
        txt = h1.get_text(" ", strip=True)
        return txt or None
    return None
