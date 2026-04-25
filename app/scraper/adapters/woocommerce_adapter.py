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

_DOSAGE_SPLIT = re.compile(r'\d+(?:\.\d+)?\s*(?:mg|mcg|ug|g|iu|ml)\b(?!\s*/?\s*mol)', re.IGNORECASE)
# Detects blend labels like "10/3 mg", "5/5mg", "2.5 / 5 mg" — collapse to a
# single dose token using the first number (the headline dose vendors advertise).
_BLEND_LABEL_RE = re.compile(
    r'(\d+(?:\.\d+)?)\s*/\s*\d+(?:\.\d+)?\s*(mg|mcg|ug|g|iu|ml)\b',
    re.IGNORECASE,
)


def _split_dosage_label(label: str) -> list[str]:
    """Split concatenated strings like '5 MG 10 MG' → ['5 MG', '10 MG'].

    Blend labels of the form 'N/M unit' (e.g. '10/3 mg') collapse to a
    single token using the first number, so the variant grouping isn't
    polluted by the second component's dose.

    When there is exactly one dosage match return just the matched portion,
    not the whole label — so '10 mg single vial' → ['10 mg'] rather than
    the whole string (which would later normalise to '10 mgsinglevial').
    """
    blend = _BLEND_LABEL_RE.search(label)
    if blend:
        return [f"{blend.group(1)} {blend.group(2).lower()}"]
    matches = _DOSAGE_SPLIT.findall(label)
    if len(matches) > 1:
        return [m.strip() for m in matches]
    if len(matches) == 1:
        return [matches[0].strip()]
    return [label]  # no dosage found — return as-is


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

    Sources (in priority order):
      1. data-product_variations JSON on the <form> — classic template, authoritative
      2. ul[data-attribute_name] elements — classic template, simpler
      3. WooCommerce attributes table (shop_attributes) — works on Gutenberg block pages
      4. Variation <select> elements — Gutenberg/non-standard variable products
    """
    amounts: list[str] = []

    # Source 1: data-product_variations JSON (classic WC variable product form).
    # Note: no early return — the JSON can be truncated when WC uses AJAX loading
    # (woocommerce_ajax_variation_threshold), so we always fall through to Sources 3/4.
    form = soup.select_one("form.variations_form[data-product_variations]")
    if form:
        try:
            variations = json.loads(form.get("data-product_variations", "[]"))
            for var in variations:
                for key, val in (var.get("attributes") or {}).items():
                    if val and _is_amount_attr(key):
                        amounts.extend(_split_dosage_label(str(val).strip()))
        except Exception:
            pass

    # Source 2: attribute selector ULs (classic WC swatches)
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
            for li in ul.select("li"):
                txt = li.get_text(" ", strip=True)
                if txt:
                    amounts.extend(_split_dosage_label(txt))

    # Source 3: WooCommerce attributes table — rendered on both classic and Gutenberg pages.
    # Covers simple products where the dose is a product attribute (e.g. Size: 10mg).
    for row in soup.select(
        "table.shop_attributes tr, "
        ".woocommerce-product-attributes tr, "
        "table.woocommerce-product-attributes tr"
    ):
        th = row.select_one("th")
        td = row.select_one("td")
        if not (th and td):
            continue
        label = th.get_text(" ", strip=True)
        if not _is_amount_attr(label):
            continue
        value = td.get_text(" ", strip=True)
        if value:
            amounts.extend(_split_dosage_label(value))
    if amounts:
        return list(dict.fromkeys(amounts))

    # Source 4: variation <select> elements — Gutenberg/non-standard variable products.
    for sel in soup.select("select[name^='attribute_'], select[id^='pa_']"):
        attr_name = (sel.get("name") or sel.get("id") or "").lower()
        if not _is_amount_attr(attr_name):
            continue
        for opt in sel.select("option"):
            # Prefer display text over the value attribute: WC slugs use hyphens
            # (e.g. value="10-mg") while the text is the canonical label ("10mg").
            val = (opt.get_text(" ", strip=True) or opt.get("value") or "").strip()
            if val and val.lower() not in ("", "choose an option", "select"):
                amounts.extend(_split_dosage_label(val))
    return list(dict.fromkeys(amounts))


def _is_amount_attr(name: str) -> bool:
    """True if the attribute name likely represents a dosage/weight/size.

    Excludes "Molecular Weight" / "Molecular Formula" — these are chemistry
    metadata, not product dosages.
    """
    name = name.lower()
    if "molecular" in name:
        return False
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
            # WooCommerce exposes per-variation stock in the variations JSON
            var_in_stock: bool | None = None
            if "is_in_stock" in var:
                var_in_stock = bool(var.get("is_in_stock"))
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
                                in_stock=var_in_stock,
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

        # Always supplement variant_amounts from HTML elements.  The form JSON can be
        # truncated when WC uses AJAX loading (woocommerce_ajax_variation_threshold), and
        # Gutenberg block pages won't have the form at all — the <select> elements and
        # attributes table are the reliable source in both cases.
        for lbl in _extract_variant_amounts(soup):
            if lbl not in variant_amounts:
                variant_amounts.append(lbl)

        if min_price is not None:
            return AdapterResult(True, name, min_price, "USD", None,
                                 variant_amounts=variant_amounts, variants=variants,
                                 price_max=max_price, category=category, tags=tags)

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
