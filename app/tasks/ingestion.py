from __future__ import annotations

import json
from contextlib import suppress
from pathlib import Path
from typing import Any, Dict, Optional

from app.core.celery_app import celery_app
from app.exception import ServerException
from app.logger import get_logger, log_context
from app.services.pdf_embedding_pipeline import PDFEmbeddingPipeline

logger = get_logger(__name__)
pipeline = PDFEmbeddingPipeline()


@celery_app.task(name="app.tasks.ingestion.process_ingestion_document")
def process_ingestion_document(
    filename: str,
    content_type: str,
    context: Optional[Dict[str, Any]] = None,
    file_path: str = "",
) -> dict[str, str]:
    """
    Load the uploaded PDF, chunk it, and create embeddings asynchronously.
    """
    task_context = context or {}

    with log_context(**task_context):
        if not file_path:
            raise ServerException(
                "No file path supplied for ingestion task.",
                code="missing_file_path",
                detail={"uploaded_filename": filename},
            )

        pdf_path = Path(file_path)
        logger.info(
            "Processing ingestion task.",
            extra={
                "uploaded_filename": filename,
                "content_type": content_type,
                "temp_file": str(pdf_path),
            },
        )

        try:
            chunk_embeddings = pipeline.process(
                pdf_path,
                context={
                    **task_context,
                    "uploaded_filename": filename,
                },
            )
        finally:
            with suppress(Exception):
                pdf_path.unlink(missing_ok=True)

        chunk_count = len(chunk_embeddings)
        logger.info(
            "Chunking and embedding complete.",
            extra={
                "uploaded_filename": filename,
                "chunk_count": chunk_count,
                "embedding_model": pipeline.processing.embedding_model,
            },
        )
        print(  # noqa: T201 - temp diagnostic until persistence is wired up
            f"[INGESTION TEST] filename={filename} chunks={chunk_count} "
            f"model={pipeline.processing.embedding_model}"
        )

        sample_metadata = (
            chunk_embeddings[0].metadata if chunk_embeddings else {"note": "no chunks"}
        )
        logger.debug(
            "Sample chunk metadata: %s",
            json.dumps(sample_metadata),
            extra={"uploaded_filename": filename},
        )

        acknowledgement = {
            "status": "received",
            "filename": filename,
            "detail": (
                "Chunks embedded asynchronously. Storage/upsert will occur in the next step."
            ),
            "chunk_count": chunk_count,
            "embedding_model": pipeline.processing.embedding_model,
        }
        return acknowledgement
