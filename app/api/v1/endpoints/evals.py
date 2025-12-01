from __future__ import annotations

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from app.logger import get_logger
from app.services.evals.langsmith_runner import run_aapl_10k_eval

router = APIRouter()
logger = get_logger(__name__)


class EvalRequest(BaseModel):
    limit: int | None = Field(
        default=None,
        ge=1,
        description="Optionally limit to the first N rows of the eval dataset.",
    )
    model: str | None = Field(
        default=None,
        description="Optional OpenAI chat model override for answering questions.",
    )
    tenant_id: str | None = Field(
        default=None,
        description="Tenant ID to use for retrieval (defaults to service config).",
    )


@router.post(
    "/evals/aapl-10k",
    status_code=status.HTTP_200_OK,
    summary="Run LangSmith evals for the Apple 10-K dataset.",
)
async def run_aapl_10k_evals(payload: EvalRequest):
    """
    Trigger the Apple 10-K retrieval eval using LangSmith.

    This endpoint runs synchronously; it may take several minutes depending on dataset size.
    """
    logger.info(
        "Starting Apple 10-K eval.",
        extra={"limit": payload.limit, "model": payload.model, "tenant_id": payload.tenant_id},
    )
    result = run_aapl_10k_eval(limit=payload.limit, model=payload.model, tenant_id=payload.tenant_id)
    logger.info("Eval completed.", extra={"experiment_url": result.get("url")})
    return {
        "experiment_name": result.get("name"),
        "experiment_url": result.get("url"),
        "dataset": result.get("dataset_name") or "aapl-10k-retrieval",
    }
