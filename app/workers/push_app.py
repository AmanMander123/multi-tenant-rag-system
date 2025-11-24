from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request, status

from app.logger import get_logger
from app.workers.pubsub_runner import PermanentIngestionError, PubSubIngestionWorker

logger = get_logger(__name__)
worker = PubSubIngestionWorker(subscription_path=None)

app = FastAPI(title="Pub/Sub Push Worker")


@app.post("/pubsub/push", status_code=status.HTTP_200_OK)
async def handle_pubsub_push(request: Request):
    """Handle Pub/Sub HTTP push delivery and kick off ingestion processing."""
    body = await request.json()
    try:
        job = worker.parse_push_request(body)
        worker.process_job(job)
        return {"status": "ok"}
    except PermanentIngestionError:
        logger.warning("Permanent ingestion error for push message; acknowledging.")
        return {"status": "permanent_error"}
    except Exception as exc:
        logger.exception("Failed to process push message.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="transient_error",
        ) from exc


@app.get("/healthz", tags=["health"])
async def healthcheck():
    return {"status": "ok"}
