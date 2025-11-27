from __future__ import annotations

import os
import urllib.parse
from hashlib import sha256
from typing import Iterable, List, Optional, Sequence

import psycopg
from psycopg import sql
from psycopg.types.json import Json
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
            updated_at timestamptz DEFAULT NOW(),
            last_indexed_at timestamptz,
            last_schema_version text,
            last_embedding_model text,
            reindex_attempts integer DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS documents_tenant_idx ON documents (tenant_id);
        ALTER TABLE documents ADD COLUMN IF NOT EXISTS last_indexed_at timestamptz;
        ALTER TABLE documents ADD COLUMN IF NOT EXISTS last_schema_version text;
        ALTER TABLE documents ADD COLUMN IF NOT EXISTS last_embedding_model text;
        ALTER TABLE documents ADD COLUMN IF NOT EXISTS reindex_attempts integer DEFAULT 0;

        CREATE TABLE IF NOT EXISTS chunks (
            chunk_id uuid PRIMARY KEY,
            tenant_id text NOT NULL,
            document_id uuid NOT NULL,
            chunk_index integer NOT NULL,
            content text NOT NULL,
            chunk_hash text NOT NULL,
            schema_version text NOT NULL,
            embedding_model text NOT NULL,
            source_uri text,
            page_number integer,
            metadata jsonb,
            tsv tsvector,
            created_at timestamptz DEFAULT NOW(),
            updated_at timestamptz DEFAULT NOW()
        );
        CREATE UNIQUE INDEX IF NOT EXISTS chunks_tenant_hash_idx ON chunks (tenant_id, chunk_hash);
        CREATE INDEX IF NOT EXISTS chunks_document_idx ON chunks (document_id);
        CREATE INDEX IF NOT EXISTS chunks_tenant_idx ON chunks (tenant_id);
        CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON chunks USING GIN (tsv);

        CREATE TABLE IF NOT EXISTS reindex_queue (
            id bigserial PRIMARY KEY,
            tenant_id text NOT NULL,
            document_id uuid NOT NULL,
            reason text DEFAULT 'drift',
            priority integer DEFAULT 5,
            status text DEFAULT 'pending',
            attempts integer DEFAULT 0,
            last_error text,
            created_at timestamptz DEFAULT NOW(),
            updated_at timestamptz DEFAULT NOW(),
            processed_at timestamptz
        );
        CREATE UNIQUE INDEX IF NOT EXISTS reindex_queue_unique ON reindex_queue (tenant_id, document_id, reason);
        CREATE INDEX IF NOT EXISTS reindex_queue_status_idx ON reindex_queue (status, priority, created_at);
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
        last_indexed_at: str | None = None,
        last_schema_version: str | None = None,
        last_embedding_model: str | None = None,
        reindex_attempts: int | None = None,
    ) -> None:
        sql = """
        INSERT INTO documents (
            document_id, tenant_id, filename, gcs_uri,
            status, chunk_count, last_error, submitted_at,
            last_indexed_at, last_schema_version, last_embedding_model, reindex_attempts
        )
        VALUES (%(document_id)s, %(tenant_id)s, %(filename)s, %(gcs_uri)s,
                %(status)s, %(chunk_count)s, %(last_error)s, %(submitted_at)s,
                %(last_indexed_at)s, %(last_schema_version)s, %(last_embedding_model)s, %(reindex_attempts)s)
        ON CONFLICT (document_id)
        DO UPDATE SET
            status = EXCLUDED.status,
            chunk_count = EXCLUDED.chunk_count,
            last_error = EXCLUDED.last_error,
            last_indexed_at = COALESCE(EXCLUDED.last_indexed_at, documents.last_indexed_at),
            last_schema_version = COALESCE(EXCLUDED.last_schema_version, documents.last_schema_version),
            last_embedding_model = COALESCE(EXCLUDED.last_embedding_model, documents.last_embedding_model),
            reindex_attempts = COALESCE(EXCLUDED.reindex_attempts, documents.reindex_attempts),
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
            "last_indexed_at": last_indexed_at,
            "last_schema_version": last_schema_version,
            "last_embedding_model": last_embedding_model,
            "reindex_attempts": reindex_attempts,
        }
        try:
            with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
        except Exception:
            logger.exception("Failed to upsert document metadata.", extra={"document_id": document_id})
            raise

    def upsert_chunks(
        self,
        *,
        tenant_id: str,
        document_id: str,
        chunks: Sequence[ChunkEmbedding],
        schema_version: str,
        tsvector_config: str,
        source_uri: str | None = None,
    ) -> int:
        """
        Persist chunk text + metadata and maintain FTS tsvector.

        Idempotent via unique (tenant_id, chunk_hash) constraint.
        """
        if not chunks:
            return 0

        records: List[dict] = []
        for chunk in chunks:
            content = chunk.text
            chunk_hash = sha256(content.encode("utf-8")).hexdigest()
            metadata = {**(chunk.metadata or {}), "document_id": document_id}
            page_number = metadata.get("page") or metadata.get("page_number")
            try:
                page_number_int = int(page_number) if page_number is not None else None
            except (TypeError, ValueError):
                page_number_int = None

            records.append(
                {
                    "chunk_id": chunk.chunk_id,
                    "tenant_id": tenant_id,
                    "document_id": document_id,
                    "chunk_index": metadata.get("chunk_index", 0),
                    "content": content,
                    "chunk_hash": chunk_hash,
                    "schema_version": schema_version,
                    "embedding_model": metadata.get("embedding_model", settings.processing.embedding_model),
                    "source_uri": source_uri or metadata.get("source") or metadata.get("source_path"),
                    "page_number": page_number_int,
                    "metadata": Json(metadata),
                    "tsvector_config": tsvector_config,
                }
            )

        insert_sql = sql.SQL(
            """
            INSERT INTO chunks (
                chunk_id, tenant_id, document_id, chunk_index,
                content, chunk_hash, schema_version, embedding_model,
                source_uri, page_number, metadata, tsv, created_at, updated_at
            )
            VALUES (
                %(chunk_id)s, %(tenant_id)s, %(document_id)s, %(chunk_index)s,
                %(content)s, %(chunk_hash)s, %(schema_version)s, %(embedding_model)s,
                %(source_uri)s, %(page_number)s, %(metadata)s,
                to_tsvector(%(tsvector_config)s, %(content)s),
                NOW(), NOW()
            )
            ON CONFLICT (tenant_id, chunk_hash)
            DO UPDATE SET
                content = EXCLUDED.content,
                chunk_index = EXCLUDED.chunk_index,
                document_id = EXCLUDED.document_id,
                schema_version = EXCLUDED.schema_version,
                embedding_model = EXCLUDED.embedding_model,
                source_uri = COALESCE(EXCLUDED.source_uri, chunks.source_uri),
                page_number = COALESCE(EXCLUDED.page_number, chunks.page_number),
                metadata = EXCLUDED.metadata,
                tsv = EXCLUDED.tsv,
                updated_at = NOW();
            """
        )

        try:
            with self.pool.connection() as conn, conn.cursor() as cur:
                cur.executemany(insert_sql, records)
        except Exception:
            logger.exception(
                "Failed to upsert chunk records.",
                extra={"tenant_id": tenant_id, "document_id": document_id, "count": len(records)},
            )
            raise

        logger.info(
            "Upserted chunk records.",
            extra={"tenant_id": tenant_id, "document_id": document_id, "count": len(records)},
        )
        return len(records)

    def search_lexical(
        self,
        *,
        tenant_id: str,
        query: str,
        limit: int,
        tsvector_config: str,
    ) -> list[dict]:
        """BM25/FTS search across per-tenant chunk corpus."""
        sql_query = """
        WITH q AS (
            SELECT plainto_tsquery(%(config)s, %(q)s) AS query
        )
        SELECT
            c.chunk_id,
            c.document_id,
            c.content,
            c.page_number,
            c.source_uri,
            c.metadata,
            ts_rank_cd(c.tsv, q.query) AS rank
        FROM chunks c, q
        WHERE c.tenant_id = %(tenant_id)s
          AND c.tsv @@ q.query
        ORDER BY rank DESC
        LIMIT %(limit)s;
        """
        params = {
            "tenant_id": tenant_id,
            "q": query,
            "limit": limit,
            "config": tsvector_config,
        }
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql_query, params)
            rows = cur.fetchall()
        return rows

    def fetch_chunks_by_ids(
        self,
        *,
        tenant_id: str,
        chunk_ids: Sequence[str],
    ) -> list[dict]:
        """Fetch chunk content/metadata for a set of chunk IDs within a tenant."""
        if not chunk_ids:
            return []
        sql_query = """
        SELECT
            chunk_id,
            document_id,
            content,
            page_number,
            source_uri,
            metadata
        FROM chunks
        WHERE tenant_id = %(tenant_id)s
          AND chunk_id = ANY(%(chunk_ids)s::uuid[]);
        """
        params = {"tenant_id": tenant_id, "chunk_ids": list(chunk_ids)}
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql_query, params)
            rows = cur.fetchall()
        return rows

    def fetch_document(self, *, tenant_id: str, document_id: str) -> dict | None:
        """Fetch a single document row for metadata lookup."""
        sql_query = """
        SELECT document_id, tenant_id, filename, gcs_uri, status, chunk_count,
               last_error, submitted_at, updated_at,
               last_indexed_at, last_schema_version, last_embedding_model, reindex_attempts
        FROM documents
        WHERE tenant_id = %(tenant_id)s
          AND document_id = %(document_id)s;
        """
        params = {"tenant_id": tenant_id, "document_id": document_id}
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql_query, params)
            return cur.fetchone()

    def enqueue_reindex(
        self,
        *,
        tenant_id: str,
        document_id: str,
        reason: str = "drift",
        priority: int = 5,
    ) -> None:
        """Add a document to the reindex queue; idempotent by (tenant, doc, reason)."""
        insert_sql = """
        INSERT INTO reindex_queue (tenant_id, document_id, reason, priority, status)
        VALUES (%(tenant_id)s, %(document_id)s, %(reason)s, %(priority)s, 'pending')
        ON CONFLICT (tenant_id, document_id, reason)
        DO UPDATE SET
            status = 'pending',
            last_error = NULL,
            updated_at = NOW();
        """
        params = {
            "tenant_id": tenant_id,
            "document_id": document_id,
            "reason": reason,
            "priority": priority,
        }
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(insert_sql, params)

    def fetch_reindex_queue(
        self,
        *,
        limit: int,
        max_attempts: int,
        tenant_id: str | None = None,
    ) -> list[dict]:
        """
        Retrieve pending reindex items joined with document metadata.

        Ordered by priority then FIFO to minimize starvation.
        """
        sql_query = """
        SELECT q.id, q.tenant_id, q.document_id, q.reason, q.priority, q.attempts,
               q.created_at, q.updated_at,
               d.filename, d.gcs_uri, d.status, d.chunk_count
        FROM reindex_queue q
        JOIN documents d ON d.document_id = q.document_id AND d.tenant_id = q.tenant_id
        WHERE q.status = 'pending'
          AND q.attempts < %(max_attempts)s
          AND (%(tenant_id)s IS NULL OR q.tenant_id = %(tenant_id)s)
        ORDER BY q.priority DESC, q.created_at ASC
        LIMIT %(limit)s;
        """
        params = {"limit": limit, "max_attempts": max_attempts, "tenant_id": tenant_id}
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql_query, params)
            return cur.fetchall()

    def mark_reindex_started(self, *, queue_id: int) -> None:
        sql_query = """
        UPDATE reindex_queue
        SET status = 'processing',
            attempts = attempts + 1,
            updated_at = NOW()
        WHERE id = %(id)s;
        """
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql_query, {"id": queue_id})

    def mark_reindex_success(self, *, queue_id: int) -> None:
        sql_query = """
        UPDATE reindex_queue
        SET status = 'completed',
            last_error = NULL,
            processed_at = NOW(),
            updated_at = NOW()
        WHERE id = %(id)s;
        """
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql_query, {"id": queue_id})

    def mark_reindex_failure(self, *, queue_id: int, error: str | None = None) -> None:
        sql_query = """
        UPDATE reindex_queue
        SET status = 'failed',
            last_error = %(error)s,
            updated_at = NOW()
        WHERE id = %(id)s;
        """
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql_query, {"id": queue_id, "error": error})

    def find_drift_candidates(
        self,
        *,
        target_schema_version: str,
        target_embedding_model: str,
        stale_after_days: int,
        limit: int,
        tenant_id: str | None = None,
    ) -> list[dict]:
        """
        Detect documents whose chunks are out of sync with the target schema/model or stale.
        """
        sql_query = """
        SELECT DISTINCT d.document_id,
                        d.tenant_id,
                        d.filename,
                        d.gcs_uri,
                        d.status,
                        d.chunk_count,
                        d.last_indexed_at,
                        d.last_schema_version,
                        d.last_embedding_model,
                        d.updated_at
        FROM documents d
        WHERE d.status IN ('completed', 'processing')
          AND (%(tenant_id)s IS NULL OR d.tenant_id = %(tenant_id)s)
          AND (
            d.last_schema_version IS DISTINCT FROM %(target_schema)s
            OR d.last_embedding_model IS DISTINCT FROM %(target_embedding)s
            OR (d.last_indexed_at IS NULL OR d.last_indexed_at < NOW() - (%(stale_after_days)s || ' days')::interval)
            OR EXISTS (
                SELECT 1 FROM chunks c
                WHERE c.document_id = d.document_id
                  AND c.tenant_id = d.tenant_id
                  AND (c.schema_version IS DISTINCT FROM %(target_schema)s
                       OR c.embedding_model IS DISTINCT FROM %(target_embedding)s)
                LIMIT 1
            )
          )
        ORDER BY d.updated_at DESC
        LIMIT %(limit)s;
        """
        params = {
            "tenant_id": tenant_id,
            "target_schema": target_schema_version,
            "target_embedding": target_embedding_model,
            "stale_after_days": stale_after_days,
            "limit": limit,
        }
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql_query, params)
            return cur.fetchall()


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

    def dense_search(
        self,
        *,
        tenant_id: str,
        vector: list[float],
        top_k: int,
    ) -> list[dict]:
        """Run a dense vector search within the tenant namespace."""
        response = self._index.query(
            namespace=tenant_id,
            vector=vector,
            top_k=top_k,
            include_values=False,
            include_metadata=True,
        )
        matches = response.get("matches", []) if isinstance(response, dict) else response.matches
        results = []
        for match in matches or []:
            metadata = getattr(match, "metadata", None) or match.get("metadata", {})  # type: ignore[union-attr]
            score = getattr(match, "score", None) or match.get("score")  # type: ignore[union-attr]
            chunk_id = getattr(match, "id", None) or match.get("id")  # type: ignore[union-attr]
            results.append(
                {
                    "chunk_id": chunk_id,
                    "score": float(score) if score is not None else None,
                    "metadata": metadata or {},
                }
            )
        return results

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
