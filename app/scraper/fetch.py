import logging
import time
from dataclasses import dataclass, field

import httpx
from bs4 import BeautifulSoup

from app.core.config import settings
from app.scraper.adapters.common import parse_price_from_text
from app.scraper.adapters.registry import adapter_chain
from app.scraper.rate_limiter import http_get_with_retry

logger = logging.getLogger(__name__)


BLOCK_TERMS = ["captcha", "access denied", "rate limit", "robot check"]
# Cloudflare challenge pages have this distinctive text; don't flag normal pages that merely use CF analytics
CLOUDFLARE_CHALLENGE_TERMS = ["just a moment", "checking your browser", "enable javascript and cookies"]


@dataclass
class ScrapeHints:
    price_selector: str | None = None
    price_attr: str | None = None
    name_selector: str | None = None
    # Platform hint: "woocommerce"|"shopify"|"bigcommerce"|"custom"
    # Auto-detected from page HTML if None — set by admin to skip detection overhead
    platform: str | None = None
    # Dosage/variant extraction overrides (take priority over adapter auto-detection)
    dosage_selector: str | None = None   # CSS selector for variant elements
    dosage_attribute: str | None = None  # WooCommerce data-attribute_name value e.g. "attribute_mg"
    # Popup/modal dismissal: custom CSS selector for the close button (optional)
    popup_close_selector: str | None = None
    # Session / auth
    cookies: list[dict] | None = None       # pre-loaded from VendorSession
    proxy_url: str | None = None
    bypass_strategy: str | None = None


@dataclass
class ScrapeResult:
    ok: bool
    status_code: int | None
    product_name: str | None
    price: float | None
    currency: str | None
    message: str | None
    body_excerpt: str | None
    adapter: str | None = None
    # Enriched fields populated by _enrich()
    in_stock: bool | None = None
    amount_mg: float | None = None
    amount_unit: str | None = None
    price_per_mg: float | None = None
    tags: list[str] = field(default_factory=list)
    sku: str | None = None
    # Variable product: available dosage/weight options e.g. ["5 mg", "10 mg"]
    variant_amounts: list = field(default_factory=list)
    # Structured variants with per-variant price (from AdapterResult.variants)
    variants: list = field(default_factory=list)
    price_max: float | None = None


def looks_blocked(status_code: int | None, body: str | None) -> bool:
    if status_code in {403, 429, 503}:
        return True
    if not body:
        return False
    # Real block pages are short; a large 200 OK page is almost certainly real content
    if status_code == 200 and len(body) > 10000:
        return False
    lower = body.lower()
    if any(term in lower for term in BLOCK_TERMS):
        return True
    # Cloudflare challenge pages have distinctive challenge text
    if any(term in lower for term in CLOUDFLARE_CHALLENGE_TERMS):
        return True
    return False


def _fetch_http(url: str, proxy_url: str | None = None) -> tuple[int | None, str | None, str | None]:
    headers = {"User-Agent": settings.scraper_user_agent}
    proxies = {"http://": proxy_url, "https://": proxy_url} if proxy_url else None
    try:
        # Use a short-lived Client for proxy support; retry/backoff handled by http_get_with_retry
        if proxy_url:
            with httpx.Client(timeout=20.0, follow_redirects=True, headers=headers, proxies=proxies) as client:
                resp = client.get(url)
        else:
            resp = http_get_with_retry(url, headers=headers, timeout=20.0, max_retries=2)
        return resp.status_code, resp.text, None
    except Exception as exc:
        return None, None, f"request_error: {exc}"


# Common modal/popup close-button selectors (tried in order)
_POPUP_CLOSE_SELECTORS = [
    # Per-site custom (passed via hints) is tried first — see _dismiss_popups
    # Klaviyo email popups
    ".klaviyo-close-form",
    ".kl-close-button",
    ".kl-private-reset-css-add-relative .needsclick",
    # Generic dismiss patterns
    "button[aria-label='Close']",
    "button[aria-label='close']",
    "button[aria-label='Dismiss']",
    "[data-testid='close-button']",
    "[data-dismiss='modal']",
    ".modal-close",
    ".popup-close",
    ".close-popup",
    ".overlay-close",
    "a.close",
    ".fancybox-close",
    ".mfp-close",
    # "No thanks" style links inside popups
    ".klaviyo-form .needsclick[data-form-element-type='close']",
    "[class*='popup'] [class*='close']",
    "[class*='modal'] [class*='close']",
    "[id*='popup'] [class*='close']",
]


def _dismiss_popups(page, extra_selector: str | None = None) -> None:
    """Try to close any visible modal/popup.

    Strategy:
      1. Press ESC immediately — handles most JS modals instantly.
      2. Wait up to 3s for a Klaviyo/generic close button to appear, then click it.
      3. Try clicking outside the modal overlay as a last resort.
    """
    # 1. ESC key — fastest, works for most modals
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
    except Exception:
        pass

    # 2. Try close-button selectors; wait up to 3s for Klaviyo-style delayed popups
    selectors = ([extra_selector] if extra_selector else []) + _POPUP_CLOSE_SELECTORS
    for attempt in range(2):  # two passes: immediate + after short wait
        for sel in selectors:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click(timeout=2000)
                    page.wait_for_timeout(400)
                    logger.debug("Dismissed popup via selector: %s", sel)
                    return
            except Exception:
                continue
        if attempt == 0:
            page.wait_for_timeout(1500)  # wait for delayed popup

    # 3. Try close button inside Klaviyo/popup iframes
    try:
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            for sel in selectors[:8]:  # only try the most likely selectors in iframes
                try:
                    el = frame.query_selector(sel)
                    if el and el.is_visible():
                        el.click(timeout=1000)
                        page.wait_for_timeout(400)
                        logger.debug("Dismissed iframe popup via selector: %s", sel)
                        return
                except Exception:
                    continue
    except Exception:
        pass

    # 4. Click the overlay backdrop (dismisses most modals that capture clicks outside)
    try:
        overlay = page.query_selector(".modal-overlay, .popup-overlay, .mfp-bg, .fancybox-overlay, [class*='overlay']")
        if overlay and overlay.is_visible():
            overlay.click(timeout=1000)
            page.wait_for_timeout(400)
    except Exception:
        pass


def _fetch_playwright(
    url: str,
    proxy_url: str | None = None,
    cookies: list[dict] | None = None,
    bypass_strategy: str | None = None,
    popup_close_selector: str | None = None,
) -> tuple[int | None, str | None, str | None]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return None, None, f"playwright_unavailable: {exc}"

    try:
        proxy = {"server": proxy_url} if proxy_url else None
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, proxy=proxy)
            ctx = browser.new_context(user_agent=settings.scraper_user_agent)
            if cookies:
                ctx.add_cookies(cookies)
            page = ctx.new_page()
            response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2500)

            # Dismiss any popup/modal (email capture, cookie consent, etc.)
            _dismiss_popups(page, extra_selector=popup_close_selector)

            # Try to solve any CAPTCHA that appears
            if bypass_strategy and bypass_strategy != "none":
                from app.scraper.captcha_solver import solve_captcha_on_page
                if solve_captcha_on_page(page, bypass_strategy):
                    page.wait_for_load_state("networkidle", timeout=10000)

            content = page.content()
            status_code = response.status if response else None
            browser.close()
            return status_code, content, None
    except Exception as exc:
        return None, None, f"playwright_error: {exc}"


def fetch_page(url: str, hints: "ScrapeHints | None" = None) -> tuple[int | None, str | None, str | None]:
    proxy_url = hints.proxy_url if hints else None
    cookies = hints.cookies if hints else None
    bypass = hints.bypass_strategy if hints else None
    popup_sel = hints.popup_close_selector if hints else None

    # If session cookies are available, start with authenticated Playwright fetch
    if cookies:
        logger.info("[fetch] Auth Playwright fetch: %s (%d cookies)", url, len(cookies))
        sc, html, _ = _fetch_playwright(url, proxy_url=proxy_url, cookies=cookies,
                                        bypass_strategy=bypass, popup_close_selector=popup_sel)
        if html and not looks_blocked(sc, html):
            logger.info("[fetch] Auth Playwright OK status=%s len=%d", sc, len(html))
            return sc, html, None
        logger.warning("[fetch] Auth Playwright blocked/failed status=%s — falling through", sc)

    status_code = None
    html = None
    last_error = None

    for attempt in range(2):
        logger.info("[fetch] HTTP attempt %d: %s", attempt + 1, url)
        status_code, html, last_error = _fetch_http(url, proxy_url=proxy_url)
        if html and not looks_blocked(status_code, html):
            logger.info("[fetch] HTTP OK status=%s len=%d", status_code, len(html))
            return status_code, html, None
        logger.info("[fetch] HTTP attempt %d failed/blocked status=%s err=%s", attempt + 1, status_code, last_error)
        if attempt == 0:
            time.sleep(1.0)

    logger.info("[fetch] HTTP failed — switching to Playwright: %s", url)

    pw_status, pw_html, pw_error = _fetch_playwright(
        url, proxy_url=proxy_url, bypass_strategy=bypass, popup_close_selector=popup_sel
    )

    if pw_html and not looks_blocked(pw_status, pw_html):
        logger.info("[fetch] Playwright OK status=%s len=%d", pw_status, len(pw_html))
        return pw_status, pw_html, None

    logger.warning(
        "All fetches failed for %s | http_status=%s http_len=%s | pw_status=%s pw_len=%s pw_err=%s",
        url, status_code, len(html) if html else 0,
        pw_status, len(pw_html) if pw_html else 0, pw_error,
    )

    # Prefer Playwright HTML when available — it's more likely to be real page content
    best_html = pw_html or html
    return status_code or pw_status, best_html, pw_error or last_error or "blocked_or_unreadable"


def _extract_with_hints(soup: BeautifulSoup, hints: "ScrapeHints") -> tuple[float | None, str | None, str | None]:
    if not hints.price_selector:
        return None, None, None

    node = soup.select_one(hints.price_selector)
    if not node:
        return None, None, None

    if hints.price_attr and node.has_attr(hints.price_attr):
        raw_price = str(node.get(hints.price_attr, "")).strip()
    else:
        raw_price = node.get_text(" ", strip=True)

    price, currency = parse_price_from_text(raw_price)
    if price is None:
        return None, None, None

    name = None
    if hints.name_selector:
        n = soup.select_one(hints.name_selector)
        if n:
            name = n.get_text(" ", strip=True) or None

    if not name:
        name = soup.title.string.strip() if soup.title and soup.title.string else None

    return price, currency, name


def _enrich(result: ScrapeResult, soup: BeautifulSoup, html: str, hints: "ScrapeHints | None" = None) -> ScrapeResult:
    """Populate in_stock, amount_mg, price_per_mg, and tags on a successful result.
    Failures in enrichment are non-fatal — the core price result is always preserved."""
    if not result.ok:
        return result

    try:
        from app.scraper.stock_detector import detect_in_stock
        result.in_stock = detect_in_stock(soup, html)
    except Exception:
        pass

    # Apply admin-configured dosage overrides — these always win over adapter auto-detection
    try:
        import json as _json
        if hints and hints.dosage_selector:
            nodes = soup.select(hints.dosage_selector)
            admin_amounts = [n.get_text(" ", strip=True) for n in nodes if n.get_text(" ", strip=True)]
            if admin_amounts:
                result.variant_amounts = admin_amounts
        elif hints and hints.dosage_attribute:
            ul = soup.select_one(f'ul[data-attribute_name="{hints.dosage_attribute}"]')
            if ul:
                raw = ul.get("data-attribute_values", "")
                vals = _json.loads(raw) if raw else []
                if vals:
                    result.variant_amounts = [str(v).strip() for v in vals if v]
    except Exception:
        pass

    try:
        from app.scraper.amount_parser import parse_amount, compute_price_per_mg
        # 1. Try to parse amount from the product title
        if result.product_name:
            result.amount_mg, result.amount_unit = parse_amount(result.product_name)
        # 2. If title had no amount, fall back to variant_amounts extracted by the adapter
        if result.amount_mg is None and result.variant_amounts:
            for label in result.variant_amounts:
                amt, unit = parse_amount(label)
                if amt is not None:
                    result.amount_mg, result.amount_unit = amt, unit
                    break
        if result.price is not None and result.amount_mg is not None:
            result.price_per_mg = compute_price_per_mg(
                result.price, result.amount_mg, result.amount_unit or "mg"
            )
    except Exception:
        pass

    if result.product_name:
        try:
            from app.scraper.tag_extractor import extract_tags
            result.tags = extract_tags(result.product_name)
        except Exception:
            pass

    return result


def _extract_with_adapters(url: str, html: str, status_code: int | None, hints: "ScrapeHints | None" = None) -> ScrapeResult:
    soup = BeautifulSoup(html, "html.parser")

    if hints:
        hinted_price, hinted_currency, hinted_name = _extract_with_hints(soup, hints)
        if hinted_price is not None:
            result = ScrapeResult(
                ok=True,
                status_code=status_code,
                product_name=hinted_name,
                price=hinted_price,
                currency=hinted_currency or "USD",
                message=None,
                body_excerpt=html[:2000],
                adapter="vendor_hint",
            )
            return _enrich(result, soup, html, hints=hints)

    adapters = adapter_chain(url, soup, html, platform=hints.platform if hints else None)
    for adapter in adapters:
        extracted = adapter.extract(url, soup, html)
        if extracted.ok and extracted.price is not None:
            # Admin-set name_selector always wins over adapter's own name extraction
            product_name = extracted.product_name
            if hints and hints.name_selector:
                n = soup.select_one(hints.name_selector)
                if n:
                    product_name = n.get_text(" ", strip=True) or product_name
            result = ScrapeResult(
                ok=True,
                status_code=status_code,
                product_name=product_name,
                price=extracted.price,
                currency=extracted.currency,
                message=None,
                body_excerpt=html[:2000],
                adapter=adapter.name,
                variant_amounts=extracted.variant_amounts or [],
                variants=extracted.variants or [],
                price_max=extracted.price_max,
            )
            return _enrich(result, soup, html, hints=hints)

    # Also apply name_selector on failure (for logging/debug)
    title = soup.title.string.strip() if soup.title and soup.title.string else None
    if hints and hints.name_selector:
        n = soup.select_one(hints.name_selector)
        if n:
            title = n.get_text(" ", strip=True) or title
    return ScrapeResult(
        ok=False,
        status_code=status_code,
        product_name=title,
        price=None,
        currency=None,
        message="price_not_found",
        body_excerpt=html[:2000],
        adapter=(adapters[-1].name if adapters else None),
    )


# Errors that mean the domain/host is permanently unreachable — retrying wastes time.
_FATAL_NETWORK_ERRORS = (
    "ERR_NAME_NOT_RESOLVED",      # DNS failure — domain doesn't exist / is down
    "ERR_INTERNET_DISCONNECTED",  # No internet
    "ERR_NAME_CHANGED",
)

# How long to wait before each retry (index = attempt number, 0-based)
_RETRY_DELAYS = (5, 15, 30)  # seconds: wait 5s before retry 1, 15s before retry 2, etc.


def _is_retryable(error: str | None) -> bool:
    """True if the error is transient and worth retrying (timeout, connection reset, etc.)."""
    if not error:
        return False
    for fatal in _FATAL_NETWORK_ERRORS:
        if fatal in error:
            return False
    return True


def scrape_url(url: str, hints: "ScrapeHints | None" = None, max_retries: int = 3) -> ScrapeResult:
    """
    Fetch and extract a product page, retrying up to max_retries times on transient network failures.
    Retries only when no HTML was returned at all (timeout, connection reset).
    DNS failures and successful-but-no-price cases are not retried.
    """
    last_status = None
    last_error = None

    for attempt in range(max_retries + 1):
        status_code, html, error = fetch_page(url, hints=hints)
        last_status = status_code
        last_error = error

        if html:
            # Got page content — attempt extraction regardless of any fetch-layer error
            result = _extract_with_adapters(url, html, status_code, hints=hints)
            if not result.ok and not result.message and error:
                result.message = error
            return result

        # No HTML at all — decide whether to retry
        if not _is_retryable(error):
            # Fatal network error (DNS down etc.) — fail immediately
            logger.info("Non-retryable fetch failure for %s: %s", url, error)
            break

        if attempt < max_retries:
            delay = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
            logger.warning("Fetch failed for %s (attempt %d/%d), retrying in %ds — %s",
                           url, attempt + 1, max_retries + 1, delay, error)
            time.sleep(delay)
        else:
            logger.warning("All %d fetch attempts failed for %s — %s", max_retries + 1, url, error)

    return ScrapeResult(False, last_status, None, None, None, last_error or "empty_response", None, adapter=None)
