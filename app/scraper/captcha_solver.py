"""
CAPTCHA detection and solving via CapSolver API.
Supports: hCaptcha, reCAPTCHA v2, Cloudflare Turnstile.

Set CAPSOLVER_API_KEY in .env to enable.
bypass_strategy on Vendor controls which path is used:
  "capsolver_hcaptcha"   → HCaptchaTaskProxyless
  "capsolver_recaptcha"  → ReCaptchaV2TaskProxyless
  "capsolver_cloudflare" → AntiTurnstileTaskProxyless
  "playwright_stealth"   → no API call; relies on stealth browser only
  "none" / None          → skip entirely
"""
import logging
import re
import time

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

CAPSOLVER_BASE = "https://api.capsolver.com"


def _get_site_key(page, captcha_type: str) -> str | None:
    """Extract the sitekey from the live page DOM."""
    try:
        selectors = {
            "hcaptcha": "[data-sitekey]",
            "recaptcha": "[data-sitekey], .g-recaptcha",
            "cloudflare": "[data-sitekey], .cf-turnstile",
        }
        sel = selectors.get(captcha_type, "[data-sitekey]")
        node = page.query_selector(sel)
        return node.get_attribute("data-sitekey") if node else None
    except Exception:
        return None


def _detect_captcha_type(page) -> str | None:
    """Return the captcha type present on the current page, or None."""
    try:
        html = page.content().lower()
        if "hcaptcha.com" in html:
            return "hcaptcha"
        if "recaptcha/api.js" in html or "grecaptcha" in html:
            return "recaptcha"
        if re.search(r"cf-browser-verification|checking your browser|just a moment", html):
            return "cloudflare"
    except Exception:
        pass
    return None


def _create_task(task: dict) -> str | None:
    """Submit a task to CapSolver; return taskId on success."""
    if not settings.capsolver_api_key:
        return None
    try:
        resp = httpx.post(
            f"{CAPSOLVER_BASE}/createTask",
            json={"clientKey": settings.capsolver_api_key, "task": task},
            timeout=15,
        )
        data = resp.json()
        if data.get("errorId") == 0:
            return data.get("taskId")
        logger.warning("CapSolver createTask error: %s", data.get("errorDescription"))
    except Exception as exc:
        logger.error("CapSolver createTask failed: %s", exc)
    return None


def _poll_solution(task_id: str, max_wait: int = 120) -> dict | None:
    """Poll CapSolver for solution; return solution dict or None on timeout/error."""
    if not settings.capsolver_api_key:
        return None
    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(3)
        try:
            resp = httpx.post(
                f"{CAPSOLVER_BASE}/getTaskResult",
                json={"clientKey": settings.capsolver_api_key, "taskId": task_id},
                timeout=10,
            )
            data = resp.json()
            if data.get("status") == "ready":
                return data.get("solution")
            if data.get("errorId", 0) != 0:
                logger.warning("CapSolver poll error: %s", data.get("errorDescription"))
                return None
        except Exception:
            pass
    logger.warning("CapSolver timed out waiting for task %s", task_id)
    return None


def _inject_token(page, captcha_type: str, token: str) -> bool:
    """Inject the solved token into the page."""
    try:
        if captcha_type == "hcaptcha":
            page.evaluate(
                f"(t => {{ "
                f"  var el = document.querySelector('[name=\"h-captcha-response\"]'); "
                f"  if (el) el.value = t; "
                f"}})(`{token}`)"
            )
        elif captcha_type == "recaptcha":
            page.evaluate(
                f"(t => {{ "
                f"  var el = document.getElementById('g-recaptcha-response'); "
                f"  if (el) el.value = t; "
                f"}})(`{token}`)"
            )
        elif captcha_type == "cloudflare":
            page.evaluate(
                f"(t => {{ "
                f"  var el = document.querySelector('[name=\"cf-turnstile-response\"]'); "
                f"  if (el) el.value = t; "
                f"}})(`{token}`)"
            )
        logger.info("CAPTCHA token injected (%s)", captcha_type)
        return True
    except Exception as exc:
        logger.warning("Token injection failed: %s", exc)
        return False


def solve_captcha_on_page(page, bypass_strategy: str | None) -> bool:
    """
    Detect and solve CAPTCHA on the current Playwright page.
    Returns True if a token was injected, False if nothing was done.
    """
    if not bypass_strategy or bypass_strategy == "none":
        return False
    if bypass_strategy == "playwright_stealth":
        return False  # stealth is applied at browser level; nothing to inject here

    if not settings.capsolver_api_key:
        logger.warning("bypass_strategy='%s' but CAPSOLVER_API_KEY is not set", bypass_strategy)
        return False

    captcha_type = _detect_captcha_type(page)
    if not captcha_type:
        return False

    site_key = _get_site_key(page, captcha_type) or ""
    page_url = page.url

    task_map = {
        "hcaptcha":   {"type": "HCaptchaTaskProxyless",       "websiteURL": page_url, "websiteKey": site_key},
        "recaptcha":  {"type": "ReCaptchaV2TaskProxyless",    "websiteURL": page_url, "websiteKey": site_key},
        "cloudflare": {"type": "AntiTurnstileTaskProxyless",  "websiteURL": page_url, "websiteKey": site_key},
    }
    task = task_map.get(captcha_type)
    if not task:
        return False

    task_id = _create_task(task)
    if not task_id:
        return False

    solution = _poll_solution(task_id)
    if not solution:
        return False

    token = (
        solution.get("gRecaptchaResponse")
        or solution.get("token")
        or solution.get("userAgent")
    )
    if not token:
        return False

    return _inject_token(page, captcha_type, token)
