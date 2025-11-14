from __future__ import annotations

import threading
from contextlib import suppress

from fastapi import FastAPI

from app.logger import get_logger
from app.workers.pubsub_runner import PubSubIngestionWorker

logger = get_logger(__name__)
app = FastAPI(title="RAG Worker Health")

_worker_thread: threading.Thread | None = None


@app.on_event("startup")
def start_worker() -> None:
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        logger.info("Pub/Sub worker already running.")
        return

    worker = PubSubIngestionWorker()
    _worker_thread = threading.Thread(target=worker.start, name="pubsub-worker", daemon=True)
    _worker_thread.start()
    logger.info("Background Pub/Sub worker thread started.")


@app.on_event("shutdown")
def stop_worker() -> None:
    global _worker_thread
    if not _worker_thread:
        return
    with suppress(Exception):
        logger.info("Shutting down Pub/Sub worker thread.")
        # PubSub client handles cancellation via signal; we rely on Cloud Run termination.
    _worker_thread = None


@app.get("/healthz", tags=["health"])
def health_check() -> dict[str, str]:
    """Return 200 OK if the worker process is alive."""
    return {"status": "ok"}

