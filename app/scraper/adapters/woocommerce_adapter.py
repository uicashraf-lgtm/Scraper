import json
import re

from bs4 import BeautifulSoup

from app.scraper.adapters.base import AdapterResult, VariantData
from app.scraper.amount_parser import parse_amount
from app.scraper.adapters.common import (
    extract_category_from_html,
    extract_category_from_jsonld,
    extract_tags_from_html,
    parse_jsonld_candidates,
    parse_price_from_text,
    read_attr,
    read_text,
)

_DOSAGE_SPLIT = re.compile(r'\d+(?:\.\d+)?\s*(?:mg|mcg|ug|g|iu|ml)\b', re.IGNORECASE)


def _split_dosage_label(label: str) -> list[str]:
    """Split concatenated strings like '5 MG 10 MG' → ['5 MG', '10 MG']."""
    matches = _DOSAGE_SPLIT.findall(label)
    return [m.strip() for m in matches] if len(matches) > 1 else [label]


def _product_name(soup: BeautifulSoup) -> str | None:
    """WooCommerce product name: prefer h1.product_title, strip site suffix from <title>."""
    for sel in ["h1.product_title", "h1.entry-title", "h1.product-title", "h1[itemprop='name']"]:
        node = soup.select_one(sel)
        if node:
            return node.get_text(" ", strip=True) or None
    if soup.title and soup.title.string:
        raw = soup.title.string.strip()
        # Strip " | Site Name" or " - Site Name" suffix
        return re.split(r"\s*[|\-–]\s*", raw)[0].strip() or raw
    return None


def _extract_variant_amounts(soup: BeautifulSoup) -> list[str]:
    """
    Extract available dosage/weight variant labels from WooCommerce variable product pages.

    Two sources:
      1. data-product_variations JSON on the <form> — authoritative, includes prices
      2. ul[data-attribute_name] elements — simpler, just the label values
    """
    # Source 1: full variations JSON
    form = soup.select_one("form.variations_form[data-product_variations]")
    if form:
        try:
            variations = json.loads(form.get("data-product_variations", "[]"))
            amounts: list[str] = []
            for var in variations:
                for key, val in (var.get("attributes") or {}).items():
                    if val and _is_amount_attr(key):
                        amounts.extend(_split_dosage_label(str(val).strip()))
            if amounts:
                return list(dict.fromkeys(amounts))  # deduplicated, order preserved
        except Exception:
            pass

    # Source 2: attribute selector ULs
    amounts = []
    for ul in soup.select("ul[data-attribute_name]"):
        attr_name = ul.get("data-attribute_name", "").lower()
        if not _is_amount_attr(attr_name):
            continue
        raw = ul.get("data-attribute_values", "")
        try:
            vals = json.loads(raw) if raw else []
            for v in vals:
                if v:
                    amounts.extend(_split_dosage_label(str(v).strip()))
        except Exception:
            # Fall back to reading child <li> text
            for li in ul.select("li"):
                txt = li.get_text(" ", strip=True)
                if txt:
                    amounts.extend(_split_dosage_label(txt))
    return list(dict.fromkeys(amounts))


def _is_amount_attr(name: str) -> bool:
    """True if the attribute name likely represents a dosage/weight/size."""
    name = name.lower()
    return any(k in name for k in ("mg", "weight", "dose", "dosage", "size", "amount", "variant", "strength"))


def _extract_variations_prices(soup: BeautifulSoup) -> tuple[float | None, float | None, list[str], list[VariantData]]:
    """
    From data-product_variations JSON return (min_price, max_price, variant_amount_labels, structured_variants).
    Returns (None, None, [], []) when not a variable product or JSON is absent.
    """
    form = soup.select_one("form.variations_form[data-product_variations]")
    if not form:
        return None, None, [], []
    try:
        variations = json.loads(form.get("data-product_variations", "[]"))
        prices: list[float] = []
        amounts: list[str] = []
        structured: list[VariantData] = []
        for var in variations:
            var_price: float | None = None
            p = var.get("display_price") or var.get("price")
            if p is not None:
                try:
                    var_price = float(str(p))
                    prices.append(var_price)
                except Exception:
                    pass
            # Extract dosage label from attributes
            for key, val in (var.get("attributes") or {}).items():
                if val and _is_amount_attr(key):
                    labels = _split_dosage_label(str(val).strip())
                    amounts.extend(labels)
                    # Build structured variant for each dosage label
                    for label in labels:
                        dosage_val, dosage_unit = parse_amount(label)
                        if dosage_val is not None:
                            structured.append(VariantData(
                                dosage=dosage_val,
                                unit=dosage_unit or "mg",
                                price=var_price,
                            ))
        return (
            min(prices) if prices else None,
            max(prices) if prices else None,
            list(dict.fromkeys(amounts)),
            structured,
        )
    except Exception:
        return None, None, [], []


class WooCommerceAdapter:
    name = "woocommerce"

    def matches(self, url: str, soup: BeautifulSoup, body: str) -> bool:
        return "woocommerce" in body.lower()

    def extract(self, url: str, soup: BeautifulSoup, body: str) -> AdapterResult:
        name = _product_name(soup)

        # Extract category and tags from HTML and JSON-LD
        category = extract_category_from_html(soup)
        if not category:
            category = extract_category_from_jsonld(parse_jsonld_candidates(soup))
        tags = extract_tags_from_html(soup)

        # 1. Variable product: parse data-product_variations JSON for min price + amounts
        min_price, max_price, variant_amounts, variants = _extract_variations_prices(soup)
        if min_price is not None:
            return AdapterResult(True, name, min_price, "USD", None,
                                 variant_amounts=variant_amounts, variants=variants,
                                 price_max=max_price, category=category, tags=tags)

        # 2. Collect variant amounts even if no variations JSON
        if not variant_amounts:
            variant_amounts = _extract_variant_amounts(soup)

        # 3. Standard simple-product price selectors
        price_text = read_text(soup, [
            "p.price > .woocommerce-Price-amount",
            ".price > ins .woocommerce-Price-amount",  # sale price takes priority
            "span.woocommerce-Price-amount",
            "p.price",
            "div.summary p.price",
            "[itemprop='price']",
        ])
        if price_text:
            price, currency = parse_price_from_text(price_text)
            if price is not None:
                return AdapterResult(True, name, price, currency or "USD", None,
                                     variant_amounts=variant_amounts, category=category, tags=tags)

        # 4. Meta tag price
        meta_price = read_attr(soup, [
            "meta[property='product:price:amount']",
            "meta[itemprop='price']",
        ], "content")
        if meta_price:
            price, currency = parse_price_from_text(meta_price)
            if price is not None:
                return AdapterResult(True, name, price, currency or "USD", None,
                                     variant_amounts=variant_amounts, category=category, tags=tags)

        return AdapterResult(False, name, None, None, "woocommerce_price_not_found",
                             variant_amounts=variant_amounts, category=category, tags=tags)
