"""
Playwright-driven login flow for vendors that require authentication.
Supports generic username/password forms (WooCommerce /my-account style).
CAPTCHA challenges encountered during login are handled via captcha_solver.
"""
import logging
from urllib.parse import urljoin

from app.core.config import settings

logger = logging.getLogger(__name__)

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
            browser.close()

            if not cookies:
                logger.warning("[login] Login produced no cookies for %s", base_url)
                return None

            logger.info("[login] SUCCESS for %s (%d cookies stored)", base_url, len(cookies))
            return cookies

    except Exception as exc:
        logger.error("playwright_login failed for %s: %s", base_url, exc)
        return None
