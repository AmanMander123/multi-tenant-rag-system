from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Dict, Optional
from uuid import uuid4

from fastapi import UploadFile
from google.api_core import exceptions as google_exceptions
from google.cloud import pubsub_v1, storage

from app.core.config import get_settings
from app.exception import ServerException, UnsupportedDocumentTypeError
from app.logger import get_context, get_logger, log_context

ALLOWED_CONTENT_TYPES = {"application/pdf"}
SCHEMA_VERSION = "2024-09-15"

logger = get_logger(__name__)
settings = get_settings()
storage_client = storage.Client()
publisher = pubsub_v1.PublisherClient()


def register_ingestion_job(
    file: UploadFile,
    *,
    context: Optional[Dict[str, object]] = None,
) -> Dict[str, str]:
    """Upload the file to Cloud Storage, enqueue a Pub/Sub job, and return an acknowledgement."""
    filename = file.filename or "unknown.pdf"
    content_type = file.content_type or "unknown/unknown"

    if content_type not in ALLOWED_CONTENT_TYPES:
        raise UnsupportedDocumentTypeError(
            content_type=content_type,
            supported_types=ALLOWED_CONTENT_TYPES,
            detail={"filename": filename},
        )

    merged_context = {**get_context(), **(context or {})}
    tenant_id = str(merged_context.get("tenant_id") or settings.default_tenant_id)
    request_id = str(merged_context.get("request_id") or uuid4())
    document_id = str(merged_context.get("document_id") or uuid4())

    with log_context(
        request_id=request_id,
        tenant_id=tenant_id,
        uploaded_filename=filename,
        document_id=document_id,
    ):
        logger.info(
            "Uploading document to Cloud Storage.",
            extra={"bucket": settings.gcs_upload_bucket, "tenant_id": tenant_id},
        )
        gcs_uri = _upload_document_to_gcs(
            file=file,
            bucket_name=settings.gcs_upload_bucket,
            tenant_id=tenant_id,
            document_id=document_id,
            content_type=content_type,
        )

        logger.info("Publishing ingestion job to Pub/Sub.", extra={"gcs_uri": gcs_uri})
        _publish_ingestion_message(
            filename=filename,
            content_type=content_type,
            request_id=request_id,
            tenant_id=tenant_id,
            document_id=document_id,
            gcs_uri=gcs_uri,
            context=merged_context,
        )

        acknowledgement = {
            "status": "received",
            "filename": filename,
            "document_id": document_id,
            "detail": "File accepted for ingestion. Worker will process asynchronously.",
        }
        log_extra = {f"ingestion_{k}": v for k, v in acknowledgement.items()}
        logger.info("Ingestion job registered.", extra=log_extra)
        return acknowledgement


def _upload_document_to_gcs(
    *,
    file: UploadFile,
    bucket_name: str,
    tenant_id: str,
    document_id: str,
    content_type: str,
) -> str:
    blob_path = _build_blob_path(tenant_id=tenant_id, document_id=document_id, filename=file.filename)
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(blob_path)

    try:
        file.file.seek(0)
        blob.upload_from_file(file.file, content_type=content_type, rewind=True)
    except google_exceptions.GoogleAPIError as exc:
        logger.exception("Failed to upload to Cloud Storage.", extra={"blob": blob_path})
        raise ServerException(
            "Unable to upload document to Cloud Storage.",
            code="storage_upload_failed",
            detail={"blob_path": blob_path},
        ) from exc

    gcs_uri = f"gs://{bucket_name}/{blob_path}"
    logger.info("Upload complete.", extra={"gcs_uri": gcs_uri})
    return gcs_uri


def _publish_ingestion_message(
    *,
    filename: str,
    content_type: str,
    request_id: str,
    tenant_id: str,
    document_id: str,
    gcs_uri: str,
    context: Dict[str, object],
) -> None:
    chunk_config = {
        "size": settings.processing.chunk_size,
        "overlap": settings.processing.chunk_overlap,
    }
    submitted_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "version": SCHEMA_VERSION,
        "request_id": request_id,
        "tenant_id": tenant_id,
        "document_id": document_id,
        "filename": filename,
        "gcs_uri": gcs_uri,
        "content_type": content_type,
        "chunk_config": chunk_config,
        "submitted_at": submitted_at,
        "attributes": {
            "source": "api-v1",
            "ingestion_mode": context.get("ingestion_mode", "standard"),
        },
    }

    pubsub_attributes = {
        "tenant_id": tenant_id,
        "request_id": request_id,
        "priority": str(context.get("priority", "standard")),
        "schema_version": SCHEMA_VERSION,
        "auth_subject": str(context.get("auth_subject", "anonymous")),
    }

    try:
        future = publisher.publish(
            settings.pubsub_topic_ingest,
            json.dumps(payload).encode("utf-8"),
            **pubsub_attributes,
        )
        future.result(timeout=30)
    except Exception as exc:  # noqa: BLE001 - ensure surfaced as server errors
        logger.exception("Failed to publish ingestion message.", extra={"topic": settings.pubsub_topic_ingest})
        raise ServerException(
            "Unable to publish ingestion job.",
            code="pubsub_publish_failed",
            detail={"topic": settings.pubsub_topic_ingest},
        ) from exc


def _build_blob_path(*, tenant_id: str, document_id: str, filename: Optional[str]) -> str:
    safe_filename = filename or "document.pdf"
    today = datetime.now(timezone.utc)
    return f"{tenant_id}/{today:%Y/%m/%d}/{document_id}-{safe_filename}"
