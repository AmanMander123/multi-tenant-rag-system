from __future__ import annotations

import os
import urllib.parse
from typing import Iterable, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pinecone import Pinecone, ServerlessSpec

from app.core.config import get_settings
from app.logger import get_logger
from app.services.pdf_embedding_pipeline import ChunkEmbedding

logger = get_logger(__name__)
settings = get_settings()


class MetadataRepository:
    """Writes ingestion metadata + statuses into Supabase Postgres."""

    def __init__(self) -> None:
        self._connection_string = _build_connection_dsn()
        self._pool: ConnectionPool | None = None
        self._tables_ready = False

    @property
    def pool(self) -> ConnectionPool:
        if self._pool is None:
            logger.info("Initializing Supabase connection pool.")
            self._pool = ConnectionPool(
                conninfo=self._connection_string,
                max_size=5,
                kwargs={"autocommit": True},
            )
        if not self._tables_ready:
            self._ensure_tables()
        return self._pool

    def _ensure_tables(self) -> None:
        ddl = """
        CREATE TABLE IF NOT EXISTS documents (
            document_id uuid PRIMARY KEY,
            tenant_id text NOT NULL,
            filename text NOT NULL,
            gcs_uri text NOT NULL,
            status text NOT NULL,
            chunk_count integer,
            last_error text,
            submitted_at timestamptz DEFAULT NOW(),
            updated_at timestamptz DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS documents_tenant_idx ON documents (tenant_id);
        """
        with self._pool.connection() as conn, conn.cursor() as cur:  # type: ignore[union-attr]
            cur.execute(ddl)
        self._tables_ready = True

    def upsert_document(
        self,
        *,
        document_id: str,
        tenant_id: str,
        filename: str,
        gcs_uri: str,
        status: str,
        chunk_count: int | None = None,
        last_error: str | None = None,
        submitted_at: str | None = None,
    ) -> None:
        sql = """
        INSERT INTO documents (
            document_id, tenant_id, filename, gcs_uri,
            status, chunk_count, last_error, submitted_at
        )
        VALUES (%(document_id)s, %(tenant_id)s, %(filename)s, %(gcs_uri)s,
                %(status)s, %(chunk_count)s, %(last_error)s, %(submitted_at)s)
        ON CONFLICT (document_id)
        DO UPDATE SET
            status = EXCLUDED.status,
            chunk_count = EXCLUDED.chunk_count,
            last_error = EXCLUDED.last_error,
            updated_at = NOW();
        """
        params = {
            "document_id": document_id,
            "tenant_id": tenant_id,
            "filename": filename,
            "gcs_uri": gcs_uri,
            "status": status,
            "chunk_count": chunk_count,
            "last_error": last_error,
            "submitted_at": submitted_at,
        }
        try:
            with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
        except Exception:
            logger.exception("Failed to upsert document metadata.", extra={"document_id": document_id})
            raise


class PineconeVectorStore:
    """Handles embedding persistence into Pinecone."""

    def __init__(self) -> None:
        api_key = os.environ.get("PINECONE_API_KEY")
        index_name = settings.pinecone_index_name
        if not api_key or not index_name:
            raise RuntimeError("Pinecone credentials are not configured.")

        self._client = Pinecone(api_key=api_key)
        self._ensure_index_exists(index_name)
        self._index = self._client.Index(index_name)

    def upsert_embeddings(
        self,
        *,
        tenant_id: str,
        document_id: str,
        embeddings: Iterable[ChunkEmbedding],
    ) -> int:
        vectors = []
        count = 0
        for chunk in embeddings:
            metadata = {
                **chunk.metadata,
                "document_id": document_id,
                "tenant_id": tenant_id,
            }
            vectors.append(
                {
                    "id": chunk.chunk_id,
                    "values": chunk.embedding,
                    "metadata": metadata,
                }
            )
            count += 1

        if not vectors:
            return 0

        self._index.upsert(vectors=vectors, namespace=tenant_id)
        logger.info(
            "Upserted vectors to Pinecone.",
            extra={"tenant_id": tenant_id, "document_id": document_id, "count": count},
        )
        return count

    def _ensure_index_exists(self, name: str) -> None:
        indexes = {i["name"] for i in self._client.list_indexes()}
        if name in indexes:
            return
        logger.info(
            "Creating Pinecone index.",
            extra={
                "index": name,
                "cloud": settings.pinecone_cloud,
                "region": settings.pinecone_region,
            },
        )
        self._client.create_index(
            name=name,
            dimension=settings.pinecone_dimension,
            metric="cosine",
            spec=ServerlessSpec(
                cloud=settings.pinecone_cloud,
                region=settings.pinecone_region,
            ),
        )


def _build_connection_dsn() -> str:
    db_url = os.environ.get("SUPABASE_DB_URL")
    db_password = os.environ.get("SUPABASE_DB_PASSWORD")
    if not db_url or not db_password:
        raise RuntimeError("Supabase DB credentials are not configured.")

    parsed = urllib.parse.urlparse(db_url)
    if not parsed.username or not parsed.hostname:
        raise RuntimeError("Invalid Supabase DB URL.")

    netloc = f"{parsed.username}:{db_password}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"

    rebuilt = parsed._replace(netloc=netloc)
    return rebuilt.geturl()
