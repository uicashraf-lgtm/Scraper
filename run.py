# run.py
import asyncio
import subprocess
import sys
import threading
import time

# Set event loop policy BEFORE importing anything else
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn

_worker_proc = None
_shutdown = False


def _spawn_worker():
    global _worker_proc
    kwargs = {}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    _worker_proc = subprocess.Popen(
        [sys.executable, "run_worker.py"],
        stdout=sys.stdout,
        stderr=sys.stderr,
        **kwargs,
    )
    print(f"[run.py] Worker process started (pid={_worker_proc.pid})", flush=True)


def _worker_watchdog():
    """Restart the worker if it crashes."""
    while not _shutdown:
        time.sleep(3)
        if _shutdown:
            break
        if _worker_proc is not None and _worker_proc.poll() is not None:
            exit_code = _worker_proc.returncode
            print(f"[run.py] Worker exited (code={exit_code}) — restarting...", flush=True)
            _spawn_worker()


def _stop_worker():
    if _worker_proc and _worker_proc.poll() is None:
        print("[run.py] Stopping worker process...")
        _worker_proc.terminate()
        try:
            _worker_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _worker_proc.kill()


if __name__ == "__main__":
    _spawn_worker()

    watchdog = threading.Thread(target=_worker_watchdog, daemon=True)
    watchdog.start()

    config = uvicorn.Config("app.main:app", host="0.0.0.0", port=8002, reload=False)
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    try:
        while thread.is_alive():
            thread.join(timeout=0.5)
    except KeyboardInterrupt:
        print("[run.py] Ctrl+C — shutting down...")
        _shutdown = True
        server.should_exit = True
        thread.join(timeout=5)
    finally:
        _shutdown = True
        _stop_worker()