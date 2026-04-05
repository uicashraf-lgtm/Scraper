"""
Rate limiting and retry helpers for outbound HTTP requests.

Covers two scenarios:
  1. WooCommerce API crawls — paginated API calls that must not flood the vendor.
  2. Web-page fetches — httpx GETs that may hit 429 / 5xx.

Key behaviours
  - 429  → honour Retry-After header; otherwise exponential back-off + jitter.
  - 5xx  → exponential back-off + jitter.
  - Network errors (timeout, connection reset) → same back-off schedule.
  - Inter-page delay → short random sleep between paginated requests.
"""

import logging
import random
import time
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

# ── tunables ──────────────────────────────────────────────────────────────────
MAX_RETRIES   = 4        # attempts AFTER the first one
BASE_BACKOFF  = 1.5      # seconds; doubled each retry
MAX_BACKOFF   = 120.0    # hard cap on any single sleep
JITTER_RANGE  = 0.5      # ± seconds added randomly to every sleep

PAGE_DELAY_MIN = 0.4     # minimum inter-page sleep (seconds) during pagination
PAGE_DELAY_MAX = 1.2     # maximum inter-page sleep


# ── public helpers ────────────────────────────────────────────────────────────

def page_delay() -> None:
    """Sleep a short random interval between paginated API requests."""
    time.sleep(random.uniform(PAGE_DELAY_MIN, PAGE_DELAY_MAX))


def http_get_with_retry(
    url: str,
    *,
    auth=None,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: float = 30.0,
    follow_redirects: bool = True,
    max_retries: int = MAX_RETRIES,
) -> httpx.Response:
    """
    httpx.get with automatic retry on 429 and 5xx responses, and on transient
    network errors (timeout, connection reset, etc.).

    Returns the last response received (caller decides what to do with 4xx).
    Raises the last httpx exception only if every attempt raised one.
    """
    backoff = BASE_BACKOFF
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            resp = httpx.get(
                url,
                auth=auth,
                params=params,
                headers=headers,
                timeout=timeout,
                follow_redirects=follow_redirects,
            )

            if resp.status_code == 429:
                if attempt >= max_retries:
                    logger.warning("[rate_limiter] 429 from %s — max retries reached", url)
                    return resp
                wait = _retry_wait(resp, backoff)
                logger.warning(
                    "[rate_limiter] 429 from %s — backing off %.1fs (attempt %d/%d)",
                    url, wait, attempt + 1, max_retries + 1,
                )
                time.sleep(wait)
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue

            if resp.status_code >= 500:
                if attempt >= max_retries:
                    logger.warning("[rate_limiter] HTTP %d from %s — max retries reached",
                                   resp.status_code, url)
                    return resp
                wait = _jitter(min(backoff, MAX_BACKOFF))
                logger.warning(
                    "[rate_limiter] HTTP %d from %s — backing off %.1fs (attempt %d/%d)",
                    resp.status_code, url, wait, attempt + 1, max_retries + 1,
                )
                time.sleep(wait)
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue

            return resp  # 2xx / 3xx / 4xx (non-429) — return immediately

        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            last_exc = exc
            if attempt >= max_retries:
                logger.warning("[rate_limiter] Network error for %s — max retries reached: %s",
                               url, exc)
                raise
            wait = _jitter(min(backoff, MAX_BACKOFF))
            logger.warning(
                "[rate_limiter] Network error for %s: %s — backing off %.1fs (attempt %d/%d)",
                url, exc, wait, attempt + 1, max_retries + 1,
            )
            time.sleep(wait)
            backoff = min(backoff * 2, MAX_BACKOFF)

    # Should not be reached, but satisfy type-checker
    if last_exc:
        raise last_exc
    raise RuntimeError(f"http_get_with_retry: logic error for {url}")


# ── internal ──────────────────────────────────────────────────────────────────

def _jitter(seconds: float) -> float:
    return max(0.0, seconds + random.uniform(-JITTER_RANGE, JITTER_RANGE))


def _parse_retry_after(resp: httpx.Response) -> float | None:
    """
    Parse the Retry-After header. Supports:
      - integer / decimal seconds  e.g. "30" or "1.5"
      - HTTP-date                  e.g. "Wed, 21 Oct 2015 07:28:00 GMT"
    Returns None when the header is absent or unparseable.
    """
    raw = resp.headers.get("Retry-After", "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(raw)
        delta = (dt - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delta)
    except Exception:
        return None


def _retry_wait(resp: httpx.Response, backoff: float) -> float:
    """Return the sleep duration for a 429 response."""
    server_wait = _parse_retry_after(resp)
    # Use whichever is larger: server hint vs. our own back-off, capped at MAX_BACKOFF
    base = max(server_wait or 0.0, backoff)
    return _jitter(min(base, MAX_BACKOFF))