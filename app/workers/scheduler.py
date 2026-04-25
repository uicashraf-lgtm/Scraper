"""
Periodic crawl scheduler.
Runs as a daemon thread alongside the worker queue loop.

Every POLL_INTERVAL_SECONDS it:
  1. Ensures every enabled vendor has a ScheduledCrawl row (auto-creates with DEFAULT_INTERVAL_HOURS).
  2. Enqueues crawl_vendor jobs for any vendor whose interval has elapsed.

No manual schedule setup is required — all enabled vendors are crawled automatically.
Per-vendor interval can still be overridden via PATCH /api/admin/schedules/{id}.
"""
import logging
import threading
import time
from datetime import datetime, timedelta

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.entities import BrokenLinkRun, ScheduledCrawl, Vendor
from app.services.queue import enqueue_broken_link_check, enqueue_vendor_crawl
from app.services.trustpilot_refresh import refresh_due_vendors

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_HOURS = settings.scheduler_interval_hours
POLL_INTERVAL_SECONDS = settings.scheduler_poll_seconds  # check every N seconds


def _sync_and_run(db):
    now = datetime.utcnow()

    # Load all enabled vendors and their schedule rows in one pass
    vendors = db.query(Vendor).filter(Vendor.enabled.is_(True)).all()
    if not vendors:
        return

    vendor_ids = [v.id for v in vendors]
    existing = {
        row.vendor_id: row
        for row in db.query(ScheduledCrawl).filter(ScheduledCrawl.vendor_id.in_(vendor_ids)).all()
    }

    enqueued_count = 0
    for vendor in vendors:
        row = existing.get(vendor.id)

        if row is None:
            row = ScheduledCrawl(
                vendor_id=vendor.id,
                interval_hours=DEFAULT_INTERVAL_HOURS,
                enabled=True,
            )
            db.add(row)
            existing[vendor.id] = row

        if not row.enabled:
            continue

        due = (
            row.last_enqueued_at is None
            or (row.last_enqueued_at + timedelta(hours=row.interval_hours)) <= now
        )
        if due:
            try:
                enqueue_vendor_crawl(vendor.id)
                row.last_enqueued_at = now
                enqueued_count += 1
                logger.info(
                    "Scheduled crawl enqueued: vendor='%s' (id=%d) interval=%dh",
                    vendor.name, vendor.id, row.interval_hours,
                )
            except Exception as exc:
                logger.error("Failed to enqueue crawl for vendor_id=%d: %s", vendor.id, exc)

    db.commit()
    if enqueued_count:
        logger.info("Scheduler: %d vendor(s) enqueued this cycle", enqueued_count)


def _run_due_schedules():
    db = SessionLocal()
    try:
        _sync_and_run(db)
    except Exception as exc:
        logger.error("Scheduler poll error: %s", exc)
        db.rollback()
    finally:
        db.close()


def _refresh_trustpilot(stop_event: threading.Event | None = None):
    db = SessionLocal()
    try:
        refresh_due_vendors(db, stop_event=stop_event)
    except Exception as exc:
        logger.error("Trustpilot refresh error: %s", exc)
        db.rollback()
    finally:
        db.close()


def _maybe_enqueue_broken_link_check():
    """Enqueue a broken-link audit if interval has elapsed since the last run."""
    db = SessionLocal()
    try:
        latest = (
            db.query(BrokenLinkRun)
            .order_by(BrokenLinkRun.started_at.desc())
            .first()
        )
        cutoff = datetime.utcnow() - timedelta(hours=settings.broken_link_check_interval_hours)
        if latest is not None and latest.started_at >= cutoff:
            return
        enqueue_broken_link_check()
        logger.info(
            "Scheduler: broken-link audit enqueued (interval=%dh, last_run=%s)",
            settings.broken_link_check_interval_hours,
            latest.started_at.isoformat() if latest else "never",
        )
    except Exception as exc:
        logger.error("Broken-link scheduler error: %s", exc)
        db.rollback()
    finally:
        db.close()


def _should_stop(stop_event: threading.Event | None) -> bool:
    return stop_event is not None and stop_event.is_set()


def scheduler_loop(stop_event: threading.Event | None = None):
    logger.info(
        "Scheduler started. Price crawls every %dh, Trustpilot refresh every %dh, "
        "broken-link audit every %dh. Poll interval: %ds.",
        DEFAULT_INTERVAL_HOURS,
        settings.trustpilot_refresh_hours,
        settings.broken_link_check_interval_hours,
        POLL_INTERVAL_SECONDS,
    )
    while not _should_stop(stop_event):
        try:
            _run_due_schedules()
        except Exception as exc:
            logger.error("Unhandled scheduler error: %s", exc)
        if _should_stop(stop_event):
            break
        try:
            _refresh_trustpilot(stop_event=stop_event)
        except Exception as exc:
            logger.error("Unhandled Trustpilot refresh error: %s", exc)
        if _should_stop(stop_event):
            break
        try:
            _maybe_enqueue_broken_link_check()
        except Exception as exc:
            logger.error("Unhandled broken-link scheduler error: %s", exc)
        # Sleep in 1s slices so a stop signal wakes us immediately.
        for _ in range(POLL_INTERVAL_SECONDS):
            if _should_stop(stop_event):
                break
            time.sleep(1)
    logger.info("Scheduler loop exited cleanly.")
