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


class RetrievalSettings(BaseModel):
    """Hybrid retrieval configuration (dense + lexical + rerank)."""

    dense_top_n: int = Field(
        default=20,
        ge=1,
        le=200,
        description="Number of dense hits to fetch from the vector store.",
    )
    bm25_top_m: int = Field(
        default=20,
        ge=1,
        le=200,
        description="Number of lexical hits to fetch from the BM25 index.",
    )
    rerank_top_k: int = Field(
        default=8,
        ge=1,
        le=50,
        description="Number of final contexts to return after reranking.",
    )
    reranker_model: str = Field(
        default="gpt-4o-mini",
        description="OpenAI model used as a cross-encoder style reranker.",
    )
    reranker_provider: Literal["openai"] = Field(
        default="openai",
        description="Provider for reranking calls.",
    )
    reranker_timeout_seconds: int = Field(
        default=10,
        ge=1,
        le=60,
        description="Timeout for reranker requests.",
    )
    chunk_schema_version: str = Field(
        default="2024-09-24",
        description="Schema version for chunk persistence and indexing.",
    )
    tsvector_config: str = Field(
        default="english",
        description="Postgres text search configuration for BM25/FTS.",
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
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
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
    pubsub_topic_ingest: str = Field(
        default="projects/virtual-assistant-460209/topics/ingestion-documents",
        description="Fully qualified Pub/Sub topic for ingestion jobs.",
    )
    gcs_upload_bucket: str = Field(
        default="va-rag-uploads-prod",
        description="Cloud Storage bucket handling raw document uploads.",
    )
    default_tenant_id: str = Field(
        default="default",
        description="Fallback tenant identifier when authentication is absent.",
    )
    supabase_url: str = Field(
        default="https://virtualassistant460209.supabase.co",
        description="Base Supabase project URL.",
    )
    supabase_jwks_url: str = Field(
        default="https://virtualassistant460209.supabase.co/auth/v1/jwks",
        description="Supabase JWKS endpoint used for JWT verification.",
    )
    supabase_jwt_audience: str = Field(
        default="auth.virtualassistant460209.supabase.co",
        description="Expected audience claim for Supabase-issued JWTs.",
    )
    supabase_auth_required: bool = Field(
        default=False,
        description="Whether every request must provide a valid Supabase JWT.",
    )
    firestore_collection_namespace: str = Field(
        default="tenants",
        description="Firestore collection name for tenant metadata.",
    )
    pinecone_index_name: str | None = Field(
        default="rag-embeddings-prod-gcp-1a",
        description="Pinecone index name for embeddings persistence.",
    )
    pinecone_cloud: str = Field(
        default="aws",
        description="Cloud provider for the Pinecone serverless index.",
    )
    pinecone_region: str = Field(
        default="us-east-1",
        description="Region for the Pinecone serverless index.",
    )
    pinecone_dimension: int = Field(
        default=1536,
        description="Embedding vector dimension used to configure Pinecone.",
    )
    pubsub_topic_ingest: str = Field(
        default="projects/virtual-assistant-460209/topics/ingestion-documents",
        description="Fully qualified Pub/Sub topic for ingestion jobs.",
    )
    gcs_upload_bucket: str = Field(
        default="va-rag-uploads-prod",
        description="Cloud Storage bucket handling raw document uploads.",
    )
    default_tenant_id: str = Field(
        default="default",
        description="Fallback tenant identifier when none supplied.",
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
