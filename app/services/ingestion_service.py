from __future__ import annotations

import shutil
from contextlib import suppress
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Dict, Optional

from fastapi import UploadFile

from app.exception import ServerException, UnsupportedDocumentTypeError
from app.logger import get_context, get_logger, log_context
from app.tasks.ingestion import process_ingestion_document

ALLOWED_CONTENT_TYPES = {"application/pdf"}
logger = get_logger(__name__)


def register_ingestion_job(
    file: UploadFile,
    *,
    context: Optional[Dict[str, object]] = None,
) -> Dict[str, str]:
    """Persist the upload, enqueue background processing, and acknowledge receipt."""
    filename = file.filename or "unknown.pdf"
    content_type = file.content_type or "unknown/unknown"

    if content_type not in ALLOWED_CONTENT_TYPES:
        raise UnsupportedDocumentTypeError(
            content_type=content_type,
            supported_types=ALLOWED_CONTENT_TYPES,
            detail={"filename": filename},
        )

    merged_context = {**get_context(), **(context or {})}
    temp_file = _persist_upload_for_worker(file, filename)

    with log_context(**merged_context):
        logger.info(
            "Registering ingestion job.",
            extra={
                "uploaded_filename": filename,
                "content_type": content_type,
                "temp_file": str(temp_file),
            },
        )

        try:
            task = process_ingestion_document.apply_async(
                kwargs={
                    "filename": filename,
                    "content_type": content_type,
                    "context": merged_context,
                    "file_path": str(temp_file),
                }
            )
        except Exception as exc:  # noqa: BLE001 - capture Celery transport failures
            logger.exception(
                "Failed to enqueue ingestion task.",
                extra={"uploaded_filename": filename, "content_type": content_type},
            )
            _safe_remove_file(temp_file)
            raise ServerException(
                "Unable to submit ingestion job to Celery.",
                detail={"filename": filename},
                context={"content_type": content_type},
            ) from exc

        logger.info(
            "Ingestion job enqueued for asynchronous processing.",
            extra={
                "uploaded_filename": filename,
                "celery_task_id": task.id,
                "content_type": content_type,
                "temp_file": str(temp_file),
            },
        )

        acknowledgement = {
            "status": "received",
            "filename": filename,
            "detail": (
                "File accepted for ingestion. Chunking and embedding running in the background."
            ),
        }
        logger.debug(
            "Ingestion acknowledgement payload ready: %s",
            acknowledgement,
            extra={"uploaded_filename": filename},
        )
        return acknowledgement


def _persist_upload_for_worker(file: UploadFile, filename: str) -> Path:
    """Write the uploaded PDF to a temporary location accessible by the worker."""
    suffix = Path(filename).suffix or ".pdf"
    temp_path: Path | None = None

    try:
        with NamedTemporaryFile(
            prefix="ingestion_", suffix=suffix, delete=False
        ) as temp_file:
            file.file.seek(0)
            shutil.copyfileobj(file.file, temp_file)
            temp_path = Path(temp_file.name)
    except Exception as exc:  # noqa: BLE001 - ensure consistent error handling
        if temp_path:
            _safe_remove_file(temp_path)
        raise ServerException(
            "Unable to persist upload for background processing.",
            code="file_persistence_error",
            detail={"filename": filename},
        ) from exc
    finally:
        with suppress(Exception):
            file.file.seek(0)

    if temp_path is None:
        raise ServerException(
            "Temporary file was not created for upload.",
            code="file_persistence_error",
            detail={"filename": filename},
        )

    logger.debug(
        "Upload persisted for worker hand-off.",
        extra={"uploaded_filename": filename, "temp_file": str(temp_path)},
    )
    return temp_path


def _safe_remove_file(path: Optional[Path]) -> None:
    if path is None:
        return
    with suppress(Exception):
        path.unlink(missing_ok=True)
