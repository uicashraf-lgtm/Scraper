import sys
import signal
import threading
import logging

if sys.platform.startswith("win"):
    import asyncio
    # Proactor loop is required for subprocess support (Playwright) on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

stop_event = threading.Event()


def _handle_shutdown(_signum, _frame):
    logger.info("Shutdown signal received. Stopping...")
    stop_event.set()


from app.workers.runner import run_worker_loop
from app.workers.scheduler import scheduler_loop

if __name__ == "__main__":
    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    # Scheduler runs in a daemon thread — dies automatically when main thread exits
    sched_thread = threading.Thread(
        target=scheduler_loop,
        kwargs={"stop_event": stop_event},
        daemon=True,
        name="scheduler",
    )
    sched_thread.start()
    print("Scheduler thread started.")
    print("Worker started. Waiting for jobs... (Ctrl+C to stop)")

    try:
        run_worker_loop(stop_event=stop_event)
    except KeyboardInterrupt:
        pass

    # Give the scheduler up to ~45s to finish its current Playwright fetch
    # before we exit. Without this, daemon-thread teardown can abort Chromium
    # mid-call and the process exits with SIGABRT, which systemd flags as a
    # failure.
    logger.info("Waiting for scheduler to finish current work...")
    sched_thread.join(timeout=45)
    if sched_thread.is_alive():
        logger.warning("Scheduler did not finish within timeout; exiting anyway.")

    logger.info("Worker stopped.")
    sys.exit(0)
