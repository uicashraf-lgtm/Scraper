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
import time
from datetime import datetime, timedelta

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.entities import ScheduledCrawl, Vendor
from app.services.queue import enqueue_vendor_crawl

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


def scheduler_loop():
    logger.info(
        "Scheduler started. All enabled vendors crawled every %dh. Poll interval: %ds.",
        DEFAULT_INTERVAL_HOURS,
        POLL_INTERVAL_SECONDS,
    )
    while True:
        try:
            _run_due_schedules()
        except Exception as exc:
            logger.error("Unhandled scheduler error: %s", exc)
        time.sleep(POLL_INTERVAL_SECONDS)
