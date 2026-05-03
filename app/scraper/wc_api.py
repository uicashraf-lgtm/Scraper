"""
WooCommerce REST API and Store API product fetcher.

Two API flavours are supported:
  - Admin REST API  (/wp-json/wc/v3/products)       — requires consumer key+secret
  - Store API       (/wp-json/wc/store/v1/products)  — public, no auth needed
    Price encoding: integer cents divided by 10**currency_minor_unit
    e.g. "12800" with minor_unit=2 → $128.00
"""
import json
import logging
import re

from app.scraper.rate_limiter import http_get_with_retry, page_delay

logger = logging.getLogger(__name__)

_AMOUNT_RE = re.compile(r'\d+(?:\.\d+)?\s*(?:mg|mcg|ug|g|iu|ml)\b(?!\s*/?\s*mol)', re.IGNORECASE)
_SLUG_HYPHEN_RE = re.compile(r'(\d)-([a-zA-Z])')
_HTML_TAG_RE = re.compile(r'<[^>]+>')
# Matches "20mg Total", "Total: 20mg", "30 mg total" — for blend product descriptions
_TOTAL_DOSE_RE = re.compile(
    r'(?:total[:\s]+)?(\d+(?:\.\d+)?)\s*(mg|mcg|ug|g|iu|ml)\s*total\b'
    r'|total\s*(?:blend\s*)?[:\-]?\s*(\d+(?:\.\d+)?)\s*(mg|mcg|ug|g|iu|ml)',
    re.IGNORECASE,
)
# Detects blend labels like "10/3 mg", "5/5mg", "2.5 / 5 mg" — the first
# number is the headline (matches what vendors advertise as the product dose).
_BLEND_RE = re.compile(
    r'(\d+(?:\.\d+)?)\s*/\s*\d+(?:\.\d+)?\s*(mg|mcg|ug|g|iu|ml)\b',
    re.IGNORECASE,
)


def _IS_AMOUNT_ATTR(name: str) -> bool:
    """True if the attribute name represents a dosage/size, not molecular metadata."""
    n = (name or "").lower()
    # Molecular Weight / Molecular Formula are chemistry metadata, not dosages.
    if "molecular" in n:
        return False
    return any(k in n for k in ("mg", "weight", "dose", "dosage", "size", "amount", "variant", "variation", "strength", "content"))


def _clean_dosage_label(label: str) -> str:
    """Normalise WooCommerce slug-format dosage labels to canonical form.

    WC variation attribute *values* from the Store API are often URL slugs
    (e.g. "10-mg", "5-mcg") rather than display labels ("10mg", "5mcg").
    Strip the slug hyphen so downstream parsing works correctly.

    Examples:
        "10-mg"  → "10mg"
        "5-MG"   → "5mg"
        "25-mcg" → "25mcg"
        "10mg"   → "10mg"   (unchanged)
        "10 mg"  → "10 mg"  (unchanged — space is fine)
    """
    s = _SLUG_HYPHEN_RE.sub(r'\1\2', label.strip())
    return s


def _parse_amount(text: str) -> tuple[float | None, str | None]:
    """Parse '10 mg' or '10-mg' → (10.0, 'mg'). Returns (None, None) if no match.

    Blend labels of the form 'N/M unit' (e.g. '10/3 mg', '5/5 mg') resolve to
    the first number — that's the headline dose vendors typically advertise.
    Without this, the unit-anchored regex below would grab the second number
    (e.g. '10/3 mg' → 3) and produce phantom variants.
    """
    cleaned = _clean_dosage_label(text or "")

    blend = _BLEND_RE.search(cleaned)
    if blend:
        return float(blend.group(1)), blend.group(2).lower()

    m = _AMOUNT_RE.search(cleaned)
    if not m:
        return None, None
    num = re.search(r'\d+(?:\.\d+)?', m.group())
    unit = re.sub(r'[\d\s.]+', '', m.group()).strip().lower() or "mg"
    return float(num.group()), unit


def _to_float(val) -> float | None:
    try:
        return float(val) if val else None
    except (ValueError, TypeError):
        return None


def is_wc_api_available(base_url: str) -> bool:
    """
    Check if the public WooCommerce REST API is available (no auth needed).
    Returns True if the endpoint responds with a product list.
    """
    endpoint = base_url.rstrip("/") + "/wp-json/wc/v3/products"
    try:
        resp = http_get_with_retry(endpoint, params={"per_page": 1}, timeout=10, max_retries=2)
        logger.info("[wc_api] Public API probe %s → HTTP %s", endpoint, resp.status_code)
        if resp.status_code == 200:
            data = resp.json()
            return isinstance(data, list)
        return False
    except Exception as exc:
        logger.info("[wc_api] Public API probe failed for %s: %s", base_url, exc)
        return False


def fetch_wc_products(
    base_url: str,
    consumer_key: str | None = None,
    consumer_secret: str | None = None,
) -> list[dict]:
    """
    Fetch all published products via WooCommerce REST API (paginated).
    If consumer_key/secret provided → authenticated (private vendor API).
    If not → unauthenticated (public WooCommerce API).
    Returns empty list if the API is unavailable or unauthorised.
    """
    endpoint = base_url.rstrip("/") + "/wp-json/wc/v3/products"
    auth = (consumer_key, consumer_secret) if consumer_key else None
    all_products: list[dict] = []
    page = 1

    while True:
        try:
            resp = http_get_with_retry(
                endpoint,
                auth=auth,
                params={"per_page": 100, "page": page, "status": "publish"},
                timeout=30,
            )
            logger.info("[wc_api] GET %s page=%d auth=%s → HTTP %s",
                        endpoint, page, bool(auth), resp.status_code)
            if resp.status_code in (401, 403):
                logger.warning("[wc_api] API requires auth for %s (HTTP %s)", base_url, resp.status_code)
                break
            if resp.status_code == 429:
                logger.error("[wc_api] Still 429 after retries for %s — aborting", endpoint)
                break
            if resp.status_code != 200:
                logger.error("[wc_api] HTTP %s from %s: %s", resp.status_code, endpoint, resp.text[:300])
                break
            products = resp.json()
            if not isinstance(products, list):
                logger.warning("[wc_api] Unexpected response type from %s", endpoint)
                break
            if not products:
                break
            logger.info("[wc_api] Page %d: %d products", page, len(products))
            all_products.extend(products)
            if len(products) < 100:
                break
            page += 1
            page_delay()  # avoid flooding the vendor with rapid page requests
        except Exception as exc:
            logger.error("[wc_api] Fetch failed at page %d: %s", page, exc)
            break

    logger.info("[wc_api] Total products fetched from %s: %d", base_url, len(all_products))
    return all_products


def fetch_wc_variations(
    base_url: str,
    product_id: int,
    consumer_key: str | None = None,
    consumer_secret: str | None = None,
) -> list[dict]:
    """Fetch price/attribute variations for a WooCommerce variable product."""
    endpoint = f"{base_url.rstrip('/')}/wp-json/wc/v3/products/{product_id}/variations"
    auth = (consumer_key, consumer_secret) if consumer_key else None
    try:
        resp = http_get_with_retry(
            endpoint,
            auth=auth,
            params={"per_page": 100},
            timeout=20,
        )
        if resp.status_code == 200:
            return resp.json()
        logger.warning("[wc_api] Variations HTTP %s for product_id=%d", resp.status_code, product_id)
    except Exception as exc:
        logger.error("[wc_api] Variations fetch failed for product_id=%d: %s", product_id, exc)
    return []


def _extract_variant_amounts(variations: list[dict]) -> list[str]:
    """Extract unique dosage labels from variation attributes."""
    seen: set[str] = set()
    amounts: list[str] = []
    for var in variations:
        for attr in (var.get("attributes") or []):
            if _IS_AMOUNT_ATTR(attr.get("name", "")):
                val = str(attr.get("option", "")).strip()
                if val and val not in seen:
                    seen.add(val)
                    amounts.append(val)
    return amounts


def process_wc_product(
    product: dict,
    base_url: str,
    consumer_key: str | None = None,
    consumer_secret: str | None = None,
) -> dict:
    """
    Convert a WC API product into a normalised listing dict.
    For variable products, fetches variations to get min price and all dosage labels.

    Returns:
        url, name, price, currency, in_stock, amount_mg, amount_unit,
        variant_amounts (list[str]), tags (list[str]), category (str|None)
    """
    name = product.get("name", "")
    url = product.get("permalink", "")
    in_stock = product.get("stock_status") == "instock"
    tags = [t["name"] for t in (product.get("tags") or []) if t.get("name")]
    category = None
    cats = product.get("categories") or []
    if cats:
        category = cats[0].get("name")

    variant_amounts: list[str] = []
    variants: list[dict] = []  # [{"dosage": float, "unit": str, "price": float|None}]
    price: float | None = None
    price_max: float | None = None

    if product.get("type") == "variable":
        logger.info("[wc_api] Fetching variations for '%s' (id=%d)", name, product["id"])
        variations = fetch_wc_variations(base_url, product["id"], consumer_key, consumer_secret)
        if variations:
            prices = [_to_float(v.get("price") or v.get("regular_price")) for v in variations]
            prices = [p for p in prices if p is not None]
            price = min(prices) if prices else None
            price_max = max(prices) if prices else None
            variant_amounts = _extract_variant_amounts(variations)
            # Build structured variants: pair each variation's dosage with its price
            for var in variations:
                var_price = _to_float(var.get("price") or var.get("regular_price"))
                var_in_stock: bool | None = None
                if "stock_status" in var:
                    var_in_stock = (var.get("stock_status") == "instock")
                for attr in (var.get("attributes") or []):
                    if _IS_AMOUNT_ATTR(attr.get("name", "")):
                        label = str(attr.get("option", "")).strip()
                        if label:
                            dosage_val, dosage_unit = _parse_amount(label)
                            if dosage_val is not None:
                                variants.append({"dosage": dosage_val, "unit": dosage_unit or "mg", "price": var_price, "in_stock": var_in_stock})
            logger.info("[wc_api]   → %d variations, price_min=%s, price_max=%s, amounts=%s",
                        len(variations), price, price_max, variant_amounts)

    if price is None:
        price = _to_float(product.get("price") or product.get("regular_price") or product.get("sale_price"))

    # Parse amount_mg from name or first variant label
    amount_mg, amount_unit = None, None
    for text in ([name] + variant_amounts):
        amount_mg, amount_unit = _parse_amount(text)
        if amount_mg is not None:
            break

    return {
        "url": url,
        "name": name,
        "price": price,
        "price_max": price_max,
        "currency": "USD",
        "in_stock": in_stock,
        "amount_mg": amount_mg,
        "amount_unit": amount_unit,
        "variant_amounts": variant_amounts,
        "variants": variants,
        "tags": tags,
        "category": category,
        "sku": product.get("sku") or None,
    }


# ─── WooCommerce Store API (public, no auth) ──────────────────────────────────

def fetch_wc_store_products(base_url: str) -> list[dict]:
    """
    Fetch all products via WooCommerce Store API (public, no authentication).
    Tries /wp-json/wc/store/v1/products first; falls back to the legacy
    /wp-json/wc/store/products path for older WooCommerce Blocks versions.
    Returns empty list if unavailable.
    """
    _ENDPOINTS = [
        base_url.rstrip("/") + "/wp-json/wc/store/v1/products",
        base_url.rstrip("/") + "/wp-json/wc/store/products",
    ]
    endpoint = _ENDPOINTS[0]
    all_products: list[dict] = []
    page = 1

    while True:
        try:
            resp = http_get_with_retry(
                endpoint,
                params={"per_page": 100, "page": page},
                timeout=30,
            )
            logger.info("[wc_store] GET %s page=%d → HTTP %s", endpoint, page, resp.status_code)
            if resp.status_code == 404 and endpoint == _ENDPOINTS[0]:
                # v1 path not found — try the legacy Store API path
                endpoint = _ENDPOINTS[1]
                logger.info("[wc_store] v1 endpoint not found, retrying with legacy path: %s", endpoint)
                continue
            if resp.status_code in (401, 403, 404):
                logger.warning("[wc_store] Store API unavailable for %s (HTTP %s)", base_url, resp.status_code)
                break
            if resp.status_code == 429:
                logger.error("[wc_store] Still 429 after retries for %s — aborting", endpoint)
                break
            if resp.status_code != 200:
                logger.error("[wc_store] HTTP %s from %s: %s", resp.status_code, endpoint, resp.text[:300])
                break
            products = resp.json()
            if not isinstance(products, list):
                logger.warning("[wc_store] Unexpected response type from %s", endpoint)
                break
            if not products:
                break
            logger.info("[wc_store] Page %d: %d products", page, len(products))
            all_products.extend(products)
            if len(products) < 100:
                break
            page += 1
            page_delay()  # avoid flooding the vendor with rapid page requests
        except Exception as exc:
            logger.error("[wc_store] Fetch failed at page %d: %s", page, exc)
            break

    logger.info("[wc_store] Total products fetched from %s: %d", base_url, len(all_products))
    return all_products


def fetch_wc_store_product_by_url(product_url: str, base_url: str) -> dict | None:
    """Fetch a single Store API product matching the URL's slug.

    Used by the per-listing crawl path so single-listing refreshes get the
    same per-variation in_stock/price data as the batch vendor crawl.
    Returns None when the slug can't be derived or the API doesn't have it.
    """
    from urllib.parse import urlparse

    path = urlparse(product_url).path.rstrip("/")
    slug = path.rsplit("/", 1)[-1] if path else ""
    if not slug:
        return None

    _SLUG_ENDPOINTS = [
        base_url.rstrip("/") + "/wp-json/wc/store/v1/products",
        base_url.rstrip("/") + "/wp-json/wc/store/products",
    ]
    for endpoint in _SLUG_ENDPOINTS:
        try:
            resp = http_get_with_retry(endpoint, params={"slug": slug}, timeout=15, max_retries=2)
            if resp.status_code == 404 and endpoint == _SLUG_ENDPOINTS[0]:
                logger.info("[wc_store] single-product v1 not found for %s, trying legacy path", base_url)
                continue
            if resp.status_code != 200:
                logger.info("[wc_store] single-product fetch %s slug=%s → HTTP %s",
                            endpoint, slug, resp.status_code)
                return None
            items = resp.json()
            if isinstance(items, list) and items:
                return items[0]
            return None
        except Exception as exc:
            logger.warning("[wc_store] single-product fetch failed for %s: %s", product_url, exc)
            return None
    return None


def _store_price(prices: dict) -> float | None:
    """Convert Store API price dict to a float. Handles both simple and variable (price_range)."""
    minor_unit = prices.get("currency_minor_unit", 2)
    divisor = 10 ** minor_unit

    raw = None
    pr = prices.get("price_range")
    if pr:
        raw = pr.get("min_amount") or pr.get("max_amount")
    # Fall back to flat price when price_range is absent or has null amounts
    if raw is None:
        raw = prices.get("price") or prices.get("regular_price")

    try:
        return int(raw) / divisor if raw is not None else None
    except (ValueError, TypeError):
        return None


def _sale_price_from_html(price_html: str) -> float | None:
    """Extract the active (discounted) price from WooCommerce price_html.

    The Store API prices{} object returns the regular price even when on_sale=True.
    The price_html always has the correct rendered price: the active/discounted price
    is in an <ins> that is NOT nested inside a <del>.

    Pattern:
      <del>...<ins aria-hidden="true">$original</ins>...</del>
      <ins><span class="woocommerce-Price-amount">$sale</span></ins>
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(price_html, "html.parser")
    for ins in reversed(soup.find_all("ins")):
        if ins.find_parent("del"):
            continue
        text = ins.get_text(" ", strip=True)
        m = re.search(r'[\d,]+(?:\.\d{1,2})?', text)
        if m:
            try:
                return float(m.group().replace(",", ""))
            except ValueError:
                pass
    return None


def _fetch_store_variation_prices(base_url: str, variations: list[dict]) -> list[dict]:
    """
    Fetch individual variation prices from the Store API.
    Each variation dict has 'id' and 'attributes'.
    Returns list of {"id", "price", "attributes"} dicts.
    """
    results = []
    for var in variations:
        vid = var.get("id")
        if not vid:
            continue
        _var_endpoints = [
            f"{base_url.rstrip('/')}/wp-json/wc/store/v1/products/{vid}",
            f"{base_url.rstrip('/')}/wp-json/wc/store/products/{vid}",
        ]
        fetched = False
        for endpoint in _var_endpoints:
            try:
                resp = http_get_with_retry(endpoint, timeout=15, max_retries=2)
                if resp.status_code == 404 and endpoint == _var_endpoints[0]:
                    logger.info("[wc_store] Variation %d v1 not found, trying legacy path", vid)
                    continue
                if resp.status_code == 200:
                    data = resp.json()
                    p_obj = data.get("prices") or {}
                    minor_unit = p_obj.get("currency_minor_unit", 2)
                    raw = p_obj.get("price") or p_obj.get("regular_price")
                    var_price = None
                    if raw is not None:
                        try:
                            var_price = int(raw) / (10 ** minor_unit)
                        except (ValueError, TypeError):
                            pass
                    # Per-variation stock from the Store API single-product response
                    var_in_stock: bool | None = None
                    if "is_in_stock" in data:
                        var_in_stock = bool(data.get("is_in_stock"))
                    elif "is_purchasable" in data:
                        var_in_stock = bool(data.get("is_purchasable"))
                    results.append({
                        "id": vid,
                        "price": var_price,
                        "in_stock": var_in_stock,
                        "attributes": var.get("attributes", []),
                    })
                    logger.info("[wc_store] Variation %d → price=%s in_stock=%s", vid, var_price, var_in_stock)
                    fetched = True
                else:
                    logger.warning("[wc_store] Variation %d → HTTP %s", vid, resp.status_code)
                break
            except Exception as exc:
                logger.warning("[wc_store] Failed to fetch variation %d: %s", vid, exc)
                break
        page_delay()
    return results


def process_wc_store_product(product: dict, base_url: str | None = None) -> dict:
    """
    Normalise a WooCommerce Store API product into a listing dict.
    Price encoding: integer string / 10**currency_minor_unit → float dollars.
    For variable products, fetches per-variation prices when base_url is provided.
    """
    name = product.get("name", "")
    url = product.get("permalink", "")
    in_stock = bool(product.get("is_in_stock", False))
    sku = product.get("sku") or None
    tags = [t["name"] for t in (product.get("tags") or []) if t.get("name")]
    category = None
    cats = product.get("categories") or []
    if cats:
        category = cats[0].get("name")

    prices_obj = product.get("prices") or {}
    price = _store_price(prices_obj)
    currency = prices_obj.get("currency_code", "USD")

    # The Store API prices{} object returns the regular_price even when on_sale=True.
    # Parse the actual discount price from price_html instead.
    if product.get("on_sale") and product.get("price_html"):
        sale_price = _sale_price_from_html(product["price_html"])
        if sale_price is not None:
            price = sale_price

    variant_amounts: list[str] = []
    variants: list[dict] = []
    price_max: float | None = None

    # Handle variable products: fetch per-variation prices
    raw_variations = product.get("variations") or []
    if raw_variations and base_url and product.get("type") == "variable":
        # Extract dosage labels from attributes
        for attr in (product.get("attributes") or []):
            if attr.get("has_variations") and _IS_AMOUNT_ATTR(attr.get("name", "")):
                for term in (attr.get("terms") or []):
                    label = _clean_dosage_label(str(term.get("name", "")).strip())
                    if label:
                        variant_amounts.append(label)

        logger.info("[wc_store] Fetching %d variation prices for '%s'", len(raw_variations), name)
        var_details = _fetch_store_variation_prices(base_url, raw_variations)

        # Build price + stock lookups by variation ID
        var_price_map = {v["id"]: v["price"] for v in var_details}
        var_stock_map = {v["id"]: v.get("in_stock") for v in var_details}

        all_prices = [p for p in var_price_map.values() if p is not None]
        if all_prices:
            price = min(all_prices)
            price_max = max(all_prices)

        # Build structured variants using attributes from list endpoint + fetched prices.
        # Also backfill variant_amounts from raw_variations in case attributes[].terms[]
        # on the list endpoint only returned the default/selected term (a common WC behaviour
        # where the full term list is only available on the single-product endpoint).
        for var in raw_variations:
            vid = var.get("id")
            var_price = var_price_map.get(vid)
            var_in_stock = var_stock_map.get(vid)
            for attr in var.get("attributes", []):
                if _IS_AMOUNT_ATTR(attr.get("name", "")):
                    # Store API variation values are often URL slugs ("10-mg");
                    # clean them to canonical form ("10mg") before storing.
                    label = _clean_dosage_label(str(attr.get("value", "")).strip())
                    if label:
                        if label not in variant_amounts:
                            variant_amounts.append(label)
                        dosage_val, dosage_unit = _parse_amount(label)
                        if dosage_val is not None:
                            variants.append({
                                "dosage": dosage_val,
                                "unit": dosage_unit or "mg",
                                "price": var_price,
                                "in_stock": var_in_stock,
                            })

        logger.info("[wc_store]   → %d variations, price_min=%s, price_max=%s, amounts=%s",
                    len(var_details), price, price_max, variant_amounts)

    # Fallback for simple products: read dose from attributes[].terms[].name
    # (variable products already populate variant_amounts above via the variations block)
    if not variant_amounts:
        for attr in (product.get("attributes") or []):
            if _IS_AMOUNT_ATTR(attr.get("name", "")):
                for term in (attr.get("terms") or []):
                    label = str(term.get("name", "")).strip()
                    if label:
                        variant_amounts.append(label)

    # Parse amount_mg from product name or first variant label
    amount_mg, amount_unit = None, None
    for text in ([name] + variant_amounts):
        amount_mg, amount_unit = _parse_amount(text)
        if amount_mg is not None:
            break

    # Fallback: extract total dose from description for blend products whose
    # name contains no dose (e.g. "BPC/TB Blend (Wolverine)" with
    # description heading "BPC-157 (10mg) + TB-500 (10mg) Blend | 20mg Total").
    if amount_mg is None:
        for field in ("short_description", "description"):
            raw_html = product.get(field) or ""
            if not raw_html:
                continue
            plain = _HTML_TAG_RE.sub(" ", raw_html)
            m = _TOTAL_DOSE_RE.search(plain)
            if m:
                num = m.group(1) or m.group(3)
                unit = m.group(2) or m.group(4)
                if num:
                    try:
                        amount_mg = float(num)
                        amount_unit = (unit or "mg").lower()
                        break
                    except ValueError:
                        pass

    return {
        "url": url,
        "name": name,
        "price": price,
        "price_max": price_max,
        "currency": currency,
        "in_stock": in_stock,
        "amount_mg": amount_mg,
        "amount_unit": amount_unit,
        "variant_amounts": variant_amounts,
        "variants": variants,
        "tags": tags,
        "category": category,
        "sku": sku,
    }
