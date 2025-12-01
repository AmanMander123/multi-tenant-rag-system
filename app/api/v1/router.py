from fastapi import APIRouter

from app.api.v1.endpoints import evals, ingestion, retrieval

api_router = APIRouter()
api_router.include_router(ingestion.router, prefix="/ingestion", tags=["data-ingestion"])
api_router.include_router(retrieval.router, tags=["retrieval"])
api_router.include_router(evals.router, tags=["evals"])
