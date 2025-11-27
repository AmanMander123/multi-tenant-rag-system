from __future__ import annotations

import json
import os
import signal
import sys
import threading
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict
import base64

from google.api_core import exceptions as gcloud_exceptions
from google.cloud import pubsub_v1, storage
from google.cloud.pubsub_v1.subscriber.message import Message

from app.core.config import get_settings
from app.exception import ServerException
from app.logger import get_logger, log_context
from app.services.pdf_embedding_pipeline import PDFEmbeddingPipeline
from app.services.persistence import MetadataRepository, PineconeVectorStore

logger = get_logger(__name__)


class PermanentIngestionError(Exception):
    """Raised when a message is invalid and should be acked instead of retried."""


@dataclass(slots=True)
class IngestionJob:
    """Represents the canonical ingestion payload flowing through Pub/Sub."""

    request_id: str
    tenant_id: str
    document_id: str
    filename: str
    gcs_uri: str
    content_type: str
    submitted_at: str
    chunk_config: Dict[str, int]
    attributes: Dict[str, str]

    @property
    def logging_context(self) -> Dict[str, Any]:
        base = {
            "request_id": self.request_id,
            "tenant_id": self.tenant_id,
            "document_id": self.document_id,
            "uploaded_filename": self.filename,
        }
        return {**base, **self.attributes}


class PubSubIngestionWorker:
    """Streaming worker that consumes Pub/Sub messages and runs the embedding pipeline."""

    def __init__(self, subscription_path: str | None = None) -> None:
        self.settings = get_settings()
        self.subscription_path = subscription_path or os.environ.get(
            "PUBSUB_SUB_INGEST_WORKER"
        )

        self._subscriber: pubsub_v1.SubscriberClient | None = None
        self._storage = storage.Client()
        self._pipeline = PDFEmbeddingPipeline()
        self._metadata_repo = MetadataRepository()
        self._vector_store = PineconeVectorStore()
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Begin streaming pull and block until interrupted."""
        if not self.subscription_path:
            raise RuntimeError(
                "PUBSUB_SUB_INGEST_WORKER environment variable must be set to start streaming."
            )
        self._subscriber = pubsub_v1.SubscriberClient()
        flow_control = pubsub_v1.types.FlowControl(max_messages=5, max_bytes=20_000_000)
        streaming_future = self._subscriber.subscribe(
            self.subscription_path,
            callback=self._handle_message,
            flow_control=flow_control,
        )

        logger.info(
            "Started Pub/Sub ingestion worker.",
            extra={"subscription": self.subscription_path},
        )

        def _shutdown(signum: int, _frame) -> None:  # type: ignore[override]
            logger.info("Shutdown signal received.", extra={"signal": signum})
            self._stop_event.set()
            streaming_future.cancel()

        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, _shutdown)

        try:
            streaming_future.result()
        except KeyboardInterrupt:
            _shutdown(signal.SIGINT, None)
        finally:
            streaming_future.cancel()
            with suppress(Exception):
                if self._subscriber:
                    self._subscriber.close()
            logger.info("Pub/Sub ingestion worker stopped.")

    def _handle_message(self, message: Message) -> None:
        job: IngestionJob | None = None
        try:
            job = self._parse_job(message)
            self.process_job(job)
            message.ack()
        except PermanentIngestionError:
            # Payload is irrecoverable; ack so it can be inspected from logs/DLQ.
            message.ack()
            if job:
                with suppress(Exception):
                    self._metadata_repo.upsert_document(
                        document_id=job.document_id,
                        tenant_id=job.tenant_id,
                        filename=job.filename,
                        gcs_uri=job.gcs_uri,
                        status="failed",
                        last_error="permanent_error",
                    )
        except Exception as exc:  # noqa: BLE001 - include pipeline/storage errors
            logger.exception("Failed to process ingestion message.")
            if job:
                with suppress(Exception):
                    self._metadata_repo.upsert_document(
                        document_id=job.document_id,
                        tenant_id=job.tenant_id,
                        filename=job.filename,
                        gcs_uri=job.gcs_uri,
                        status="failed",
                        last_error=str(exc),
                    )
            message.nack()
            raise exc

    def _parse_job(self, message: Message) -> IngestionJob:
        try:
            payload = json.loads(message.data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.error("Received malformed JSON payload; acknowledging.")
            raise PermanentIngestionError from exc

        body_attributes = payload.get("attributes") or {}
        message_attributes = dict(message.attributes or {})
        combined_attributes = {**body_attributes, **message_attributes}
        payload["attributes"] = combined_attributes
        return self._parse_job_object(payload)

    def parse_push_request(self, push_body: Dict[str, Any]) -> IngestionJob:
        """Parse Pub/Sub HTTP push payload into an IngestionJob."""
        message = push_body.get("message") or {}
        data_b64 = message.get("data")
        if not data_b64:
            logger.error("Push message missing data.")
            raise PermanentIngestionError()

        try:
            decoded = base64.b64decode(data_b64).decode("utf-8")
            payload = json.loads(decoded)
        except Exception as exc:
            logger.error("Failed to decode push message payload.")
            raise PermanentIngestionError() from exc

        attributes = message.get("attributes") or {}
        body_attributes = payload.get("attributes") or {}
        payload["attributes"] = {**body_attributes, **attributes}
        return self._parse_job_object(payload)

    def _parse_job_object(self, payload: Dict[str, Any]) -> IngestionJob:
        required_fields = [
            "request_id",
            "tenant_id",
            "document_id",
            "filename",
            "gcs_uri",
            "content_type",
            "submitted_at",
        ]
        missing = [field for field in required_fields if field not in payload]
        if missing:
            logger.error("Message missing required fields: %s", missing)
            raise PermanentIngestionError()

        chunk_config: Dict[str, int] = payload.get("chunk_config") or {}
        return IngestionJob(
            request_id=str(payload["request_id"]),
            tenant_id=str(payload["tenant_id"]),
            document_id=str(payload["document_id"]),
            filename=str(payload["filename"]),
            gcs_uri=str(payload["gcs_uri"]),
            content_type=str(payload["content_type"]),
            submitted_at=str(payload["submitted_at"]),
            chunk_config={
                k: int(v) for k, v in chunk_config.items() if isinstance(v, (int, float))
            },
            attributes={k: str(v) for k, v in (payload.get("attributes") or {}).items()},
        )

    def process_job(self, job: IngestionJob) -> None:
        with log_context(**job.logging_context):
            self._metadata_repo.upsert_document(
                document_id=job.document_id,
                tenant_id=job.tenant_id,
                filename=job.filename,
                gcs_uri=job.gcs_uri,
                status="processing",
                submitted_at=job.submitted_at,
            )
            temp_pdf = self._download_pdf(job.gcs_uri)
            try:
                chunk_embeddings = self._pipeline.process(
                    temp_pdf,
                    context={
                        **job.logging_context,
                        "content_type": job.content_type,
                        "submitted_at": job.submitted_at,
                    },
                    chunk_config=job.chunk_config or None,
                )
            finally:
                with suppress(Exception):
                    temp_pdf.unlink(missing_ok=True)

            chunk_count = len(chunk_embeddings)
            self._metadata_repo.upsert_chunks(
                tenant_id=job.tenant_id,
                document_id=job.document_id,
                chunks=chunk_embeddings,
                schema_version=self.settings.retrieval.chunk_schema_version,
                tsvector_config=self.settings.retrieval.tsvector_config,
                source_uri=job.gcs_uri,
            )
            self._vector_store.upsert_embeddings(
                tenant_id=job.tenant_id,
                document_id=job.document_id,
                embeddings=chunk_embeddings,
            )
            indexed_at = datetime.now(tz=timezone.utc).isoformat()
            self._metadata_repo.upsert_document(
                document_id=job.document_id,
                tenant_id=job.tenant_id,
                filename=job.filename,
                gcs_uri=job.gcs_uri,
                status="completed",
                chunk_count=chunk_count,
                last_indexed_at=indexed_at,
                last_schema_version=self.settings.retrieval.chunk_schema_version,
                last_embedding_model=self.settings.processing.embedding_model,
            )
            logger.info(
                "Ingestion job processed.",
                extra={
                    **job.logging_context,
                    "chunk_count": chunk_count,
                    "gcs_uri": job.gcs_uri,
                },
            )

    def _download_pdf(self, gcs_uri: str) -> Path:
        bucket_name, blob_name = self._parse_gcs_uri(gcs_uri)
        bucket = self._storage.bucket(bucket_name)
        blob = bucket.blob(blob_name)

        suffix = Path(blob_name).suffix or ".pdf"
        temp_file = NamedTemporaryFile(
            prefix="pubsub_ingestion_", suffix=suffix, delete=False
        )
        temp_path = Path(temp_file.name)
        temp_file.close()

        try:
            blob.download_to_filename(str(temp_path))
        except gcloud_exceptions.NotFound as exc:
            logger.error("Referenced GCS object not found.", extra={"gcs_uri": gcs_uri})
            raise PermanentIngestionError() from exc
        except gcloud_exceptions.GoogleAPICallError as exc:
            logger.exception(
                "Transient error downloading GCS object.", extra={"gcs_uri": gcs_uri}
            )
            with suppress(Exception):
                temp_path.unlink(missing_ok=True)
            raise

        return temp_path

    @staticmethod
    def _parse_gcs_uri(gcs_uri: str) -> tuple[str, str]:
        if not gcs_uri.startswith("gs://"):
            logger.error("Invalid GCS URI format.", extra={"gcs_uri": gcs_uri})
            raise PermanentIngestionError()

        without_scheme = gcs_uri.removeprefix("gs://")
        parts = without_scheme.split("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            logger.error("Malformed GCS URI.", extra={"gcs_uri": gcs_uri})
            raise PermanentIngestionError()
        return parts[0], parts[1]


def main() -> int:
    worker = PubSubIngestionWorker()
    worker.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
