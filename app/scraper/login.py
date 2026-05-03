"""
Playwright-driven login flow for vendors that require authentication.
Supports generic username/password forms (WooCommerce /my-account style).
CAPTCHA challenges encountered during login are handled via captcha_solver.
"""
import logging
from urllib.parse import urljoin

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


def _wp_login_direct(base_url: str, email: str, password: str) -> list[dict] | None:
    """Log in via WordPress wp-login.php using a plain HTTP POST.

    Faster and more reliable than Playwright form detection for standard
    WordPress/WooCommerce sites — works regardless of how the site's frontend
    is structured (custom landing pages, page builders, membership plugins).

    Returns a list of cookie dicts containing wordpress_logged_in_* on success,
    None if wp-login.php is unreachable or credentials are rejected.
    """
    login_url = base_url.rstrip("/") + "/wp-login.php"
    try:
        with httpx.Client(follow_redirects=True, timeout=20) as client:
            client.cookies.set("wordpress_test_cookie", "WP Cookie check")
            resp = client.post(login_url, data={
                "log": email,
                "pwd": password,
                "wp-submit": "Log In",
                "redirect_to": base_url.rstrip("/") + "/",
                "testcookie": "1",
            })
            auth_cookies = [c for c in client.cookies.jar if "wordpress_logged_in" in c.name]
            if auth_cookies:
                logger.info("[login] wp-login.php SUCCESS for %s (%d cookies)", base_url, len(list(client.cookies.jar)))
                return [
                    {"name": c.name, "value": c.value, "domain": c.domain or base_url, "path": c.path or "/"}
                    for c in client.cookies.jar
                    if c.name and c.value
                ]
            logger.debug("[login] wp-login.php: no wordpress_logged_in cookie for %s (HTTP %s)", base_url, resp.status_code)
    except Exception as exc:
        logger.debug("[login] wp-login.php attempt failed for %s: %s", base_url, exc)
    return None

DEFAULT_LOGIN_PATH = "/my-account"

# Common selectors for login forms across WooCommerce, Shopify, BigCommerce
_EMAIL_SELECTORS = (
    "input[type='email']",
    "input[name='email']",
    "input[name='username']",
    "input[name='log']",      # WooCommerce
    "input[id='email']",
    "input[id='username']",
)

_PASS_SELECTORS = (
    "input[type='password']",
    "input[name='password']",
    "input[name='pwd']",      # WooCommerce
)

_SUBMIT_SELECTORS = (
    "button[type='submit']",
    "input[type='submit']",
    "button[name='login']",
    ".woocommerce-form-login__submit",
)


def _try_selector_list(page, selectors: tuple[str, ...]):
    """Return the first *visible* element that matches any selector in the list."""
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return el
        except Exception:
            continue
    return None


_LOGIN_FORM_SELECTORS = (
    # WooCommerce login form (most specific — wins over register form on same page)
    "form.woocommerce-form-login",
    "form#loginform",
    # Generic: any form whose action or id hints at login
    "form[action*='login']",
    "form[id*='login']",
    "form[class*='login']",
)

_REGISTER_FORM_SELECTORS = (
    "form.woocommerce-form-register",
    "form[action*='register']",
    "form[id*='register']",
    "form[class*='register']",
    "form[action*='signup']",
    "form[id*='signup']",
    "form[class*='signup']",
)

_EMAIL_FIELD_SELECTORS = (
    "input[name='log']",
    "input[name='username']",
    "input[name='email']",
    "input[type='email']",
    "input[id='username']",
    "input[id='email']",
)


def _find_login_form_inputs(page):
    """Find email/username and password inputs in the login form.

    Priority order:
    1. WooCommerce / known login-form selectors (avoids register/newsletter forms)
    2. Any form with a password field that is NOT a known register form
    3. Any form with a password field (last resort)

    Returns (email_el, pass_el) or (None, None) if no login form is found.
    """
    try:
        # Pass 1: known login-form CSS selectors
        for form_sel in _LOGIN_FORM_SELECTORS:
            form = page.query_selector(form_sel)
            if not form:
                continue
            pass_el = form.query_selector("input[type='password']")
            if not pass_el or not pass_el.is_visible():
                continue
            for sel in _EMAIL_FIELD_SELECTORS:
                email_el = form.query_selector(sel)
                if email_el and email_el.is_visible():
                    return email_el, pass_el

        # Collect forms that look like register forms so we can skip them
        register_forms = set()
        for reg_sel in _REGISTER_FORM_SELECTORS:
            el = page.query_selector(reg_sel)
            if el:
                register_forms.add(el)

        # Pass 2: any form with a password field that isn't a register form
        forms = page.query_selector_all("form")
        for form in forms:
            if form in register_forms:
                continue
            pass_el = form.query_selector("input[type='password']")
            if not pass_el or not pass_el.is_visible():
                continue
            for sel in _EMAIL_FIELD_SELECTORS:
                email_el = form.query_selector(sel)
                if email_el and email_el.is_visible():
                    return email_el, pass_el

        # Pass 3: last resort — any form with a password field
        for form in forms:
            pass_el = form.query_selector("input[type='password']")
            if not pass_el or not pass_el.is_visible():
                continue
            for sel in _EMAIL_FIELD_SELECTORS:
                email_el = form.query_selector(sel)
                if email_el and email_el.is_visible():
                    return email_el, pass_el
    except Exception:
        pass
    return None, None


def playwright_login(
    base_url: str,
    email: str,
    password_enc: str,
    login_url_path: str | None = None,
    bypass_strategy: str | None = None,
    proxy_url: str | None = None,
) -> list[dict] | None:
    """
    Drive a headless browser to log into a vendor site.

    Args:
        base_url: Vendor root URL (e.g. "https://genpeptide.com")
        email: Login email/username
        password_enc: Fernet-encrypted password (decrypted here)
        login_url_path: Path to login page (default: "/my-account")
        bypass_strategy: CAPTCHA strategy string
        proxy_url: Optional proxy server URL

    Returns:
        List of Playwright cookie dicts on success, None on failure.
    """
    from app.services.crypto import decrypt_password
    from app.scraper.captcha_solver import solve_captcha_on_page

    try:
        password = decrypt_password(password_enc)
    except Exception as exc:
        logger.error("Failed to decrypt password for %s: %s", base_url, exc)
        return None

    # Fast path: try wp-login.php directly before launching a browser.
    # Works for standard WordPress/WooCommerce sites regardless of frontend
    # structure (custom landing pages, membership plugins, page builders).
    # Falls through to Playwright if the site doesn't use wp-login.php or
    # the credentials are rejected there.
    wp_cookies = _wp_login_direct(base_url, email, password)
    if wp_cookies:
        return wp_cookies
    logger.info("[login] wp-login.php fast path failed for %s — falling back to Playwright", base_url)

    login_path = login_url_path or DEFAULT_LOGIN_PATH
    login_url = urljoin(base_url, login_path)
    proxy = {"server": proxy_url} if proxy_url else None

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        logger.error("Playwright not available: %s", exc)
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, proxy=proxy)
            ctx = browser.new_context(user_agent=settings.scraper_user_agent)
            page = ctx.new_page()

            logger.info("[login] Navigating to login page: %s", login_url)
            page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)
            logger.info("[login] Page loaded. Dismissing any popups...")

            # Dismiss any modal/popup before interacting with the login form
            from app.scraper.fetch import _dismiss_popups
            _dismiss_popups(page)
            logger.info("[login] Popup dismissal done. Looking for login form...")

            # Handle pre-login CAPTCHA
            solve_captcha_on_page(page, bypass_strategy)

            # Prefer a form that contains both an email/username AND a password
            # field — avoids filling newsletter/register forms that share the page.
            email_input, pass_input = _find_login_form_inputs(page)
            if not email_input or not pass_input:
                email_input = _try_selector_list(page, _EMAIL_SELECTORS)
                pass_input = _try_selector_list(page, _PASS_SELECTORS)

            if not email_input or not pass_input:
                logger.warning("[login] Login form not found at %s (email_found=%s pass_found=%s)",
                               login_url, bool(email_input), bool(pass_input))
                browser.close()
                return None

            logger.info("[login] Filling credentials for %s", base_url)
            email_input.fill(email)
            pass_input.fill(password)

            # Look for the submit button inside the same form as the password field
            form_el = pass_input.evaluate_handle("el => el.closest('form')").as_element()
            submit = None
            if form_el:
                for sel in _SUBMIT_SELECTORS:
                    try:
                        el = form_el.query_selector(sel)
                        if el and el.is_visible():
                            submit = el
                            break
                    except Exception:
                        continue
            if not submit:
                submit = _try_selector_list(page, _SUBMIT_SELECTORS)
            if submit:
                logger.info("[login] Clicking submit button")
                try:
                    submit.click(timeout=5000)
                except Exception:
                    logger.warning("[login] Submit click failed, falling back to Enter key")
                    pass_input.press("Enter")
            else:
                logger.info("[login] No visible submit button, pressing Enter")
                pass_input.press("Enter")

            logger.info("[login] Waiting for page to load after submit...")
            page.wait_for_load_state("networkidle", timeout=20000)

            # Handle post-submit CAPTCHA challenge
            solve_captcha_on_page(page, bypass_strategy)
            page.wait_for_timeout(1000)

            cookies = ctx.cookies()

            # While the browser is still open and authenticated, navigate to the
            # homepage to extract the WooCommerce Store API nonce. This nonce is
            # required alongside session cookies to pass WordPress REST auth checks.
            try:
                page.goto(base_url.rstrip("/") + "/", wait_until="domcontentloaded", timeout=15000)
                nonce = page.evaluate(
                    "() => (window.wcSettings || {}).storeApiNonce "
                    "|| (window.wc_store_api_settings || {}).nonce || null"
                )
                if nonce:
                    logger.info("[login] Extracted storeApiNonce from authenticated session")
                    cookies.append({
                        "name": "__wc_store_nonce__",
                        "value": str(nonce),
                        "domain": base_url,
                        "path": "/",
                    })
                else:
                    logger.debug("[login] storeApiNonce not found in page JS for %s", base_url)
            except Exception as exc:
                logger.debug("[login] Could not extract nonce for %s: %s", base_url, exc)

            browser.close()

            if not cookies:
                logger.warning("[login] Login produced no cookies for %s", base_url)
                return None

            logger.info("[login] SUCCESS for %s (%d cookies stored)", base_url, len(cookies))
            return cookies

    except Exception as exc:
        logger.error("playwright_login failed for %s: %s", base_url, exc)
        return None
