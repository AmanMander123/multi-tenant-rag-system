from __future__ import annotations

from functools import lru_cache

from google.cloud import secretmanager
from google.api_core.exceptions import GoogleAPIError

from app.core.config import get_settings
from app.exception import ServerException
from app.logger import get_logger

logger = get_logger(__name__)


@lru_cache(maxsize=32)
def fetch_secret(secret_name: str, version: str | None = None) -> str:
    """
    Retrieve a secret value from GCP Secret Manager.

    Args:
        secret_name: Name of the secret resource (without project path).
        version: Secret version identifier. Defaults to latest.
    """
    settings = get_settings()
    project_id = settings.gcp_project_id
    secret_version = version or settings.openai_secret_version

    resource_name = (
        f"projects/{project_id}/secrets/{secret_name}/versions/{secret_version}"
    )

    try:
        client = secretmanager.SecretManagerServiceClient()
        response = client.access_secret_version(name=resource_name)
        payload = response.payload.data.decode("utf-8")
        logger.debug(
            "Fetched secret from Secret Manager.",
            extra={"secret_name": secret_name, "project_id": project_id},
        )
        return payload
    except GoogleAPIError as exc:
        logger.exception(
            "Failed to fetch secret from Secret Manager.",
            extra={"secret_name": secret_name, "project_id": project_id},
        )
        raise ServerException(
            "Unable to load secret from GCP Secret Manager.",
            code="secret_manager_error",
            detail={"secret_name": secret_name, "project_id": project_id},
        ) from exc
