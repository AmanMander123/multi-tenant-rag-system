from fastapi import FastAPI

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.exception import register_exception_handlers
from app.middleware import SupabaseAuthMiddleware


def create_application() -> FastAPI:
    settings = get_settings()
    application = FastAPI(title=settings.project_name)

    register_exception_handlers(application)
    application.add_middleware(SupabaseAuthMiddleware)
    application.include_router(api_router, prefix=settings.api_v1_prefix)

    return application


app = create_application()


@app.get("/healthz", tags=["health"])
async def healthcheck():
    """Lightweight readiness probe."""
    return {"status": "ok"}
