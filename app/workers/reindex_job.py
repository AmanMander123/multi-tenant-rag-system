from __future__ import annotations

import argparse
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, List
from uuid import uuid4

from google.api_core import exceptions as gcloud_exceptions
from google.cloud import storage

from app.core.config import get_settings
from app.logger import get_logger, log_context
from app.services.pdf_embedding_pipeline import PDFEmbeddingPipeline
from app.services.persistence import MetadataRepository, PineconeVectorStore

logger = get_logger(__name__)


@dataclass(slots=True)
class ReindexWorkItem:
    queue_id: int | None
    tenant_id: str
    document_id: str
    filename: str
    gcs_uri: str
    reason: str
    attempts: int


class ReindexJobRunner:
    """Nightly reindex/backfill runner triggered by Cloud Scheduler."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._metadata_repo = MetadataRepository()
        self._vector_store = PineconeVectorStore()
        self._pipeline = PDFEmbeddingPipeline()
        self._storage = storage.Client()

    def run(self, *, tenant_id: str | None, limit: int, dry_run: bool) -> dict[str, Any]:
        cfg = self.settings.reindex
        # Step 1: enqueue drift candidates so they are tracked uniformly.
        drift_candidates = self._metadata_repo.find_drift_candidates(
            target_schema_version=self.settings.retrieval.chunk_schema_version,
            target_embedding_model=self.settings.processing.embedding_model,
            stale_after_days=cfg.stale_after_days,
            limit=cfg.queue_poll_limit,
            tenant_id=tenant_id,
        )
        for candidate in drift_candidates:
            self._metadata_repo.enqueue_reindex(
                tenant_id=candidate["tenant_id"],
                document_id=str(candidate["document_id"]),
                reason="drift",
                priority=5,
            )

        # Step 2: pull pending queue items.
        queue_items = self._metadata_repo.fetch_reindex_queue(
            limit=min(cfg.queue_poll_limit, limit),
            max_attempts=cfg.max_attempts,
            tenant_id=tenant_id,
        )
        work: List[ReindexWorkItem] = []
        for row in queue_items:
            work.append(
                ReindexWorkItem(
                    queue_id=int(row["id"]),
                    tenant_id=str(row["tenant_id"]),
                    document_id=str(row["document_id"]),
                    filename=str(row["filename"]),
                    gcs_uri=str(row["gcs_uri"]),
                    reason=str(row.get("reason") or "drift"),
                    attempts=int(row.get("attempts") or 0),
                )
            )

        processed = 0
        failed = 0
        skipped = 0
        start = datetime.now(tz=timezone.utc)
        for item in work[:limit]:
            if dry_run:
                skipped += 1
                logger.info(
                    "Dry-run: would reindex document.",
                    extra={
                        "tenant_id": item.tenant_id,
                        "document_id": item.document_id,
                        "gcs_uri": item.gcs_uri,
                        "reason": item.reason,
                        "queue_id": item.queue_id,
                    },
                )
                continue

            try:
                self._process_item(item)
                processed += 1
            except Exception as exc:  # noqa: BLE001 - surface in logs
                failed += 1
                if item.queue_id is not None:
                    with suppress(Exception):
                        self._metadata_repo.mark_reindex_failure(
                            queue_id=item.queue_id, error=str(exc)
                        )
                logger.exception(
                    "Failed to reindex document.",
                    extra={
                        "tenant_id": item.tenant_id,
                        "document_id": item.document_id,
                        "queue_id": item.queue_id,
                    },
                )

        duration_seconds = (datetime.now(tz=timezone.utc) - start).total_seconds()
        logger.info(
            "Reindex job completed.",
            extra={
                "processed": processed,
                "failed": failed,
                "skipped": skipped,
                "duration_seconds": duration_seconds,
                "tenant_id": tenant_id,
            },
        )
        return {
            "processed": processed,
            "failed": failed,
            "skipped": skipped,
            "duration_seconds": duration_seconds,
        }

    def _process_item(self, item: ReindexWorkItem) -> None:
        request_id = str(uuid4())
        if item.queue_id is not None:
            self._metadata_repo.mark_reindex_started(queue_id=item.queue_id)

        with log_context(
            request_id=request_id,
            tenant_id=item.tenant_id,
            document_id=item.document_id,
            reason=item.reason,
            queue_id=item.queue_id,
        ):
            temp_pdf = self._download_pdf(item.gcs_uri)
            try:
                chunk_embeddings = self._pipeline.process(
                    temp_pdf,
                    context={
                        "request_id": request_id,
                        "tenant_id": item.tenant_id,
                        "document_id": item.document_id,
                        "source_path": item.gcs_uri,
                        "reason": item.reason,
                    },
                    chunk_config=None,
                )
            finally:
                with suppress(Exception):
                    temp_pdf.unlink(missing_ok=True)

            chunk_count = len(chunk_embeddings)
            indexed_at = datetime.now(tz=timezone.utc).isoformat()
            self._metadata_repo.upsert_chunks(
                tenant_id=item.tenant_id,
                document_id=item.document_id,
                chunks=chunk_embeddings,
                schema_version=self.settings.retrieval.chunk_schema_version,
                tsvector_config=self.settings.retrieval.tsvector_config,
                source_uri=item.gcs_uri,
            )
            self._vector_store.upsert_embeddings(
                tenant_id=item.tenant_id,
                document_id=item.document_id,
                embeddings=chunk_embeddings,
            )
            self._metadata_repo.upsert_document(
                document_id=item.document_id,
                tenant_id=item.tenant_id,
                filename=item.filename,
                gcs_uri=item.gcs_uri,
                status="completed",
                chunk_count=chunk_count,
                last_indexed_at=indexed_at,
                last_schema_version=self.settings.retrieval.chunk_schema_version,
                last_embedding_model=self.settings.processing.embedding_model,
            )
            if item.queue_id is not None:
                self._metadata_repo.mark_reindex_success(queue_id=item.queue_id)
            logger.info(
                "Reindexed document.",
                extra={
                    "tenant_id": item.tenant_id,
                    "document_id": item.document_id,
                    "chunk_count": chunk_count,
                    "queue_id": item.queue_id,
                    "reason": item.reason,
                },
            )

    def _download_pdf(self, gcs_uri: str) -> Path:
        bucket_name, blob_name = self._parse_gcs_uri(gcs_uri)
        bucket = self._storage.bucket(bucket_name)
        blob = bucket.blob(blob_name)

        suffix = Path(blob_name).suffix or ".pdf"
        temp_file = NamedTemporaryFile(prefix="reindex_", suffix=suffix, delete=False)
        temp_path = Path(temp_file.name)
        temp_file.close()

        try:
            blob.download_to_filename(str(temp_path))
        except gcloud_exceptions.NotFound as exc:
            logger.error("Referenced GCS object not found.", extra={"gcs_uri": gcs_uri})
            raise
        except gcloud_exceptions.GoogleAPICallError:
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
            raise ValueError("Invalid GCS URI.")

        without_scheme = gcs_uri.removeprefix("gs://")
        parts = without_scheme.split("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            logger.error("Malformed GCS URI.", extra={"gcs_uri": gcs_uri})
            raise ValueError("Invalid GCS URI.")
        return parts[0], parts[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run nightly reindex/backfill.")
    parser.add_argument("--tenant", dest="tenant_id", help="Limit to a single tenant.")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max documents to process this run (defaults to config).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log planned work without touching storage/indexes.",
    )
    args = parser.parse_args()

    runner = ReindexJobRunner()
    max_docs = args.limit or runner.settings.reindex.max_documents
    runner.run(tenant_id=args.tenant_id, limit=max_docs, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
