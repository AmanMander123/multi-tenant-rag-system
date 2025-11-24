from __future__ import annotations

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.logger import get_logger, log_context
from app.services.retrieval_engine import HybridRetriever

router = APIRouter()
logger = get_logger(__name__)
settings = get_settings()
retriever = HybridRetriever()


class AskRequest(BaseModel):
    query: str = Field(..., description="Natural language query to retrieve supporting context for.")


@router.post(
    "/ask",
    status_code=status.HTTP_200_OK,
    summary="Hybrid retrieval across dense + BM25 + reranker.",
)
async def ask(request: Request, payload: AskRequest):
    auth_context = getattr(request.state, "auth_context", None)
    tenant_id = getattr(auth_context, "tenant_id", None) or settings.default_tenant_id
    request_id = getattr(auth_context, "request_id", None) or getattr(request.state, "request_id", None)

    with log_context(request_id=request_id, tenant_id=tenant_id):
        logger.info(
            "Received retrieval request.",
            extra={"tenant_id": tenant_id},
        )
        response = retriever.retrieve(query=payload.query, tenant_id=tenant_id)
        logger.info(
            "Retrieval completed.",
            extra={
                "tenant_id": tenant_id,
                "dense_hits": response["diagnostics"]["dense_retrieved"],
                "lexical_hits": response["diagnostics"]["lexical_retrieved"],
            },
        )
        return response
