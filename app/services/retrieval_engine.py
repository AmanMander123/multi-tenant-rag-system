from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Sequence

from langchain_openai import OpenAIEmbeddings
from openai import OpenAI

from app.core.config import get_settings
from app.core.secrets import fetch_secret
from app.logger import get_logger
from app.services.persistence import MetadataRepository, PineconeVectorStore

logger = get_logger(__name__)
settings = get_settings()


@dataclass
class Candidate:
    """Represents a merged retrieval candidate prior to reranking."""

    chunk_id: str
    document_id: str
    content: str
    source_uri: str | None
    page_number: int | None
    dense_score: float | None = None
    lexical_score: float | None = None
    metadata: Dict[str, Any] | None = None

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "content": self.content,
            "source_uri": self.source_uri,
            "page_number": self.page_number,
            "dense_score": self.dense_score,
            "lexical_score": self.lexical_score,
            "metadata": self.metadata or {},
        }


class QueryEmbedder:
    """Embed queries with the same model used for ingestion chunks."""

    def __init__(self) -> None:
        api_key = fetch_secret(settings.openai_secret_name, settings.openai_secret_version)
        if not api_key:
            raise RuntimeError("OpenAI API key missing; cannot embed queries.")
        self._embedder = OpenAIEmbeddings(
            api_key=api_key,
            model=settings.processing.embedding_model,
        )

    def embed(self, query: str) -> list[float]:
        return self._embedder.embed_query(query)


class OpenAIReranker:
    """Cross-encoder style reranker using OpenAI chat completion."""

    def __init__(self) -> None:
        api_key = fetch_secret(settings.openai_secret_name, settings.openai_secret_version)
        if not api_key:
            raise RuntimeError("OpenAI API key missing; cannot rerank.")
        self._client = OpenAI(api_key=api_key)
        self._model = settings.retrieval.reranker_model
        self._timeout = settings.retrieval.reranker_timeout_seconds

    def rerank(self, query: str, candidates: Sequence[Candidate], top_k: int) -> list[Candidate]:
        if not candidates:
            return []

        limited = list(candidates)[: max(top_k * 2, top_k)]
        rows = []
        for idx, cand in enumerate(limited, start=1):
            rows.append(
                f"{idx}. id={cand.chunk_id} doc={cand.document_id} "
                f"score_dense={cand.dense_score} score_lex={cand.lexical_score}\n"
                f"text: {cand.content[:500]}"
            )
        prompt = (
            "You are a retrieval reranker. Score each passage for relevance to the query "
            "from 0.0 (irrelevant) to 1.0 (fully relevant). Return JSON with key "
            "`scores` as a list of objects: {\"chunk_id\": str, \"score\": float}. "
            "Preserve the input chunk_id values. Only return JSON."
        )
        request_messages = [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": f"Query: {query}\n\nCandidates:\n" + "\n\n".join(rows),
            },
        ]

        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=request_messages,
                temperature=0,
                response_format={"type": "json_object"},
                timeout=self._timeout,
            )
            content = response.choices[0].message.content or "{}"
            parsed = self._safe_json_parse(content)
            scored = parsed.get("scores") or []
            score_map = {str(item.get("chunk_id")): float(item.get("score", 0)) for item in scored}
            ranked = sorted(
                limited,
                key=lambda c: score_map.get(c.chunk_id, 0),
                reverse=True,
            )
            reranked = []
            for cand in ranked:
                reranked.append(
                    Candidate(
                        chunk_id=cand.chunk_id,
                        document_id=cand.document_id,
                        content=cand.content,
                        source_uri=cand.source_uri,
                        page_number=cand.page_number,
                        dense_score=cand.dense_score,
                        lexical_score=cand.lexical_score,
                        metadata={
                            **(cand.metadata or {}),
                            "rerank_score": score_map.get(cand.chunk_id),
                        },
                    )
                )
            return reranked[:top_k]
        except Exception:
            logger.exception("Reranker failed; returning blend without rerank.")
            return list(limited)[:top_k]

    @staticmethod
    def _safe_json_parse(value: str) -> dict:
        import json

        try:
            return json.loads(value)
        except Exception:
            return {}


class HybridRetriever:
    """Hybrid dense + BM25 + rerank retrieval engine."""

    def __init__(self) -> None:
        self._embedder = QueryEmbedder()
        self._vector_store = PineconeVectorStore()
        self._metadata_repo = MetadataRepository()
        self._reranker = OpenAIReranker()
        self._cfg = settings.retrieval

    def retrieve(
        self,
        *,
        query: str,
        tenant_id: str,
    ) -> dict:
        normalized_query = query.strip()
        dense_vector = self._embedder.embed(normalized_query)
        dense_hits = self._vector_store.dense_search(
            tenant_id=tenant_id,
            vector=dense_vector,
            top_k=self._cfg.dense_top_n,
        )

        lexical_hits = self._metadata_repo.search_lexical(
            tenant_id=tenant_id,
            query=normalized_query,
            limit=self._cfg.bm25_top_m,
            tsvector_config=self._cfg.tsvector_config,
        )

        dense_chunk_ids = [hit["chunk_id"] for hit in dense_hits if hit.get("chunk_id")]
        known_chunks = {row["chunk_id"]: row for row in self._metadata_repo.fetch_chunks_by_ids(tenant_id=tenant_id, chunk_ids=dense_chunk_ids)}

        candidates: dict[str, Candidate] = {}

        # Seed lexical candidates (already include content).
        for hit in lexical_hits:
            chunk_id = str(hit["chunk_id"])
            candidates[chunk_id] = Candidate(
                chunk_id=chunk_id,
                document_id=str(hit["document_id"]),
                content=str(hit["content"]),
                source_uri=hit.get("source_uri"),
                page_number=hit.get("page_number"),
                lexical_score=float(hit.get("rank", 0)),
                dense_score=None,
                metadata=hit.get("metadata") or {},
            )

        # Enrich dense hits with chunk rows from Postgres.
        for hit in dense_hits:
            chunk_id = str(hit.get("chunk_id"))
            if not chunk_id:
                continue
            chunk_row = known_chunks.get(chunk_id)
            if not chunk_row:
                continue
            existing = candidates.get(chunk_id)
            candidates[chunk_id] = Candidate(
                chunk_id=chunk_id,
                document_id=str(chunk_row["document_id"]),
                content=str(chunk_row["content"]),
                source_uri=chunk_row.get("source_uri"),
                page_number=chunk_row.get("page_number"),
                dense_score=float(hit.get("score")) if hit.get("score") is not None else None,
                lexical_score=existing.lexical_score if existing else None,
                metadata={**(chunk_row.get("metadata") or {}), **(existing.metadata if existing else {})},
            )

        merged = list(candidates.values())
        blended_sorted = self._blend(merged)
        reranked = self._reranker.rerank(normalized_query, blended_sorted, self._cfg.rerank_top_k)

        return {
            "query": normalized_query,
            "tenant_id": tenant_id,
            "results": [c.to_dict() for c in reranked],
            "diagnostics": {
                "dense_retrieved": len(dense_hits),
                "lexical_retrieved": len(lexical_hits),
                "merged_candidates": len(merged),
                "returned": len(reranked),
            },
        }

    @staticmethod
    def _blend(candidates: Sequence[Candidate]) -> list[Candidate]:
        if not candidates:
            return []

        dense_scores = [c.dense_score for c in candidates if c.dense_score is not None]
        lexical_scores = [c.lexical_score for c in candidates if c.lexical_score is not None]

        def normalize(values: List[float]) -> Dict[float, float]:
            if not values:
                return {}
            high = max(values)
            low = min(values)
            if high == low:
                return {v: 1.0 for v in values}
            return {v: (v - low) / (high - low) for v in values}

        dense_norm = normalize(dense_scores)
        lexical_norm = normalize(lexical_scores)

        blended: list[tuple[Candidate, float]] = []
        for cand in candidates:
            dense_component = dense_norm.get(cand.dense_score, 0.0) if cand.dense_score is not None else 0.0
            lexical_component = lexical_norm.get(cand.lexical_score, 0.0) if cand.lexical_score is not None else 0.0
            # Equal weights for now; can be tuned per tenant.
            blended_score = 0.5 * dense_component + 0.5 * lexical_component
            blended.append((cand, blended_score))

        blended_sorted = [c for c, _ in sorted(blended, key=lambda pair: pair[1], reverse=True)]
        return blended_sorted
