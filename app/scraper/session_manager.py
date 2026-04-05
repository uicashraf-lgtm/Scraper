"""
Manages persistent authenticated browser sessions (Playwright cookies)
for vendors that require login before scraping.
"""
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session as DBSession

from app.models.entities import VendorSession

logger = logging.getLogger(__name__)

SESSION_TTL_HOURS = 23  # refresh before the typical 24h session expiry


def load_session(db: DBSession, vendor_id: int) -> list[dict] | None:
    """
    Return the stored cookie list if a valid, non-expired session exists.
    Returns None if missing or expired (caller should re-login).
    """
    row = db.query(VendorSession).filter(VendorSession.vendor_id == vendor_id).first()
    if not row or not row.cookies_json:
        return None
    if row.expires_at and row.expires_at < datetime.utcnow():
        logger.info("Session expired for vendor_id=%d; will re-login", vendor_id)
        return None
    try:
        return json.loads(row.cookies_json)
    except Exception:
        return None


def save_session(db: DBSession, vendor_id: int, cookies: list[dict], ttl_hours: int = SESSION_TTL_HOURS):
    """Upsert the session row for this vendor."""
    row = db.query(VendorSession).filter(VendorSession.vendor_id == vendor_id).first()
    payload = json.dumps(cookies)
    expires = datetime.utcnow() + timedelta(hours=ttl_hours)
    if row:
        row.cookies_json = payload
        row.expires_at = expires
        row.updated_at = datetime.utcnow()
    else:
        db.add(VendorSession(
            vendor_id=vendor_id,
            cookies_json=payload,
            expires_at=expires,
        ))
    db.commit()
    logger.info("Session saved for vendor_id=%d (expires %s)", vendor_id, expires.isoformat())


def invalidate_session(db: DBSession, vendor_id: int):
    """Delete the session row, forcing a re-login on the next crawl."""
    db.query(VendorSession).filter(VendorSession.vendor_id == vendor_id).delete()
    db.commit()
    logger.info("Session invalidated for vendor_id=%d", vendor_id)
