from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"


class CelerySettings(BaseModel):
    """Celery connection and queue configuration."""

    broker_url: str = Field(
        default="memory://",
        description="Celery broker URL. Defaults to in-memory transport for local development.",
    )
    result_backend: str | None = Field(
        default="cache+memory://",
        description="Celery result backend URL. Optional for fire-and-forget workflows.",
    )
    task_default_queue: str = Field(
        default="ingestion",
        description="Default queue for ingestion jobs.",
    )
    task_default_routing_key: str = Field(
        default="ingestion.default",
        description="Default routing key for ingestion jobs.",
    )


class DocumentProcessingSettings(BaseModel):
    """Controls LangChain chunking and embedding parameters."""

    chunk_size: int = Field(
        default=1_000,
        ge=200,
        le=2_000,
        description="Number of characters per chunk before embedding.",
    )
    chunk_overlap: int = Field(
        default=200,
        ge=0,
        lt=1_000,
        description="Overlap between neighbouring chunks to retain context.",
    )
    embedding_provider: Literal["openai"] = Field(
        default="openai",
        description="Embedding provider identifier. Currently supports 'openai'.",
    )
    embedding_model: str = Field(
        default="text-embedding-3-small",
        description="Embedding model identifier for the selected provider.",
    )


class Settings(BaseSettings):
    """Application configuration loaded from YAML with environment overrides."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
    )

    project_name: str
    api_v1_prefix: str
    celery: CelerySettings = Field(default_factory=CelerySettings)
    processing: DocumentProcessingSettings = Field(
        default_factory=DocumentProcessingSettings
    )
    gcp_project_id: str = Field(
        default="rag-knowledge-base-464616",
        description="Default GCP project ID used for secret access and telemetry.",
    )
    openai_secret_name: str = Field(
        default="OPENAI_API_KEY",
        description="Secret Manager name that stores the OpenAI API key.",
    )
    openai_secret_version: str = Field(
        default="latest",
        description="Secret version to read for the OpenAI API key.",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        """Merge config sources so env/.env override YAML values."""
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            cls._yaml_config_settings,
            file_secret_settings,
        )

    @staticmethod
    def _yaml_config_settings() -> dict[str, Any]:
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as file:
                data = yaml.safe_load(file) or {}
        except FileNotFoundError as exc:
            raise RuntimeError(f"Config file not found at {CONFIG_PATH}") from exc
        except yaml.YAMLError as exc:
            raise RuntimeError(f"Failed to parse configuration file {CONFIG_PATH}") from exc

        if not isinstance(data, dict):
            raise RuntimeError(
                f"Invalid configuration format in {CONFIG_PATH}: expected a mapping."
            )

        return data


@lru_cache()
def get_settings() -> Settings:
    """Return cached application settings instance."""
    return Settings()
