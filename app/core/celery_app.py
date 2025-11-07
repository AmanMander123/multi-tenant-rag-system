from __future__ import annotations

from celery import Celery

from app.core.config import get_settings
from app.logger import get_logger

logger = get_logger(__name__)

celery_app = Celery(
    "multi_tenant_rag_system",
    include=("app.tasks.ingestion",),
)


def configure_celery_app() -> Celery:
    """Configure Celery with application settings and return the instance."""
    settings = get_settings()
    celery_settings = settings.celery

    celery_config: dict[str, object] = {
        "broker_url": celery_settings.broker_url,
        "task_default_queue": celery_settings.task_default_queue,
        "task_default_routing_key": celery_settings.task_default_routing_key,
        "task_default_exchange": celery_settings.task_default_queue,
        "task_serializer": "json",
        "accept_content": ["json"],
        "result_serializer": "json",
        "enable_utc": True,
        "timezone": "UTC",
    }

    if celery_settings.result_backend:
        celery_config["result_backend"] = celery_settings.result_backend

    celery_app.conf.update(celery_config)
    celery_app.autodiscover_tasks(["app.tasks"])
    logger.debug(
        "Celery application configured.",
        extra={
            "broker_url": celery_settings.broker_url,
            "default_queue": celery_settings.task_default_queue,
        },
    )
    return celery_app


configure_celery_app()

__all__ = ("celery_app", "configure_celery_app")
