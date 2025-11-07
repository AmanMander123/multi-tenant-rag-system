from uuid import uuid4

from fastapi import APIRouter, File, UploadFile, status

from app.services import ingestion_service
from app.logger import get_logger, log_context

router = APIRouter()
logger = get_logger(__name__)


@router.post(
    "/documents",
    status_code=status.HTTP_200_OK,
    summary="Receive and queue data ingestion jobs",
)
async def ingest_document(file: UploadFile = File(...)):
    """
    Receive a PDF file upload and register it for processing.

    Currently this endpoint only validates that a file was received and
    returns a confirmation response. The ingestion_service will later
    coordinate storage, chunking, and embedding workflows.
    """
    request_id = uuid4().hex
    filename = file.filename or "unknown.pdf"
    content_type = file.content_type or "unknown/unknown"

    with log_context(request_id=request_id, uploaded_filename=filename):
        logger.info(
            "Received ingestion request.",
            extra={"content_type": content_type, "uploaded_filename": filename},
        )

        acknowledgement = ingestion_service.register_ingestion_job(
            file,
            context={
                "request_id": request_id,
                "uploaded_filename": filename,
            },
        )

        logger.info("Ingestion job registration acknowledged.")
        return acknowledgement
