"""
Detects product in-stock status from scraped HTML.
Uses a cascade: JSON-LD availability → CSS class signals → text heuristics.
"""
import json
import re

from bs4 import BeautifulSoup

_OOS_TERMS = re.compile(
    r"\b(out[\s\-]of[\s\-]stock|sold[\s\-]out|unavailable|discontinued|backordered|out of stock)\b",
    re.IGNORECASE,
)
_IN_STOCK_TERMS = re.compile(
    r"\b(in[\s\-]stock|add to cart|buy now|add_to_cart|purchase|order now)\b",
    re.IGNORECASE,
)


def detect_in_stock(soup: BeautifulSoup, html: str) -> bool | None:
    """
    Return True (in stock), False (out of stock), or None (unknown).
    """
    # 1. JSON-LD structured data — most reliable signal
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or ""
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        offers = data.get("offers")
        if isinstance(offers, list) and offers:
            offers = offers[0]
        if isinstance(offers, dict):
            avail = str(offers.get("availability", "")).lower()
            if "instock" in avail:
                return True
            if "outofstock" in avail or "discontinued" in avail or "soldout" in avail:
                return False

    # 2. WooCommerce / Shopify body class signals
    body = soup.body
    if body:
        body_classes = " ".join(body.get("class", []))
        if "out-of-stock" in body_classes or "outofstock" in body_classes:
            return False
        if "in-stock" in body_classes or "instock" in body_classes:
            return True

    # 3. Explicit out-of-stock element
    oos_el = soup.find(class_=re.compile(r"out.?of.?stock|sold.?out", re.I))
    if oos_el:
        return False

    # 4. Cart button presence (reliable in-stock signal)
    cart_btn = soup.find("button", attrs={"name": re.compile(r"add.?to.?cart|buy", re.I)})
    if cart_btn:
        disabled = cart_btn.get("disabled")
        if disabled is not None and disabled is not False and disabled != "false":
            return False
        return True

    # 5. Text heuristics on a short excerpt (avoid false positives on long pages)
    excerpt = soup.get_text(" ", strip=True)[:4000]
    if _OOS_TERMS.search(excerpt):
        return False
    if _IN_STOCK_TERMS.search(excerpt):
        return True

    return None  # Could not determine
