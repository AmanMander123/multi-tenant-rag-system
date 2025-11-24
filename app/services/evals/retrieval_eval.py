from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Protocol

from app.services.retrieval_engine import HybridRetriever


@dataclass
class EvalQuery:
    """Represents a labeled query for offline retrieval evaluation."""

    query_id: str
    tenant_id: str
    query: str
    relevant_chunk_ids: List[str]


class EvalStore(Protocol):
    """Interface for loading/saving eval datasets (qrels)."""

    def load(self) -> Iterable[EvalQuery]: ...
    def write_result(self, query: EvalQuery, metrics: dict) -> None: ...


class InMemoryEvalStore:
    """Placeholder store until Supabase/BigQuery eval tables are defined."""

    def __init__(self, queries: Iterable[EvalQuery]) -> None:
        self._queries = list(queries)

    def load(self) -> Iterable[EvalQuery]:
        return self._queries

    def write_result(self, query: EvalQuery, metrics: dict) -> None:  # noqa: D401 - placeholder writer
        # Intentionally left as a no-op; replace with Supabase/BigQuery writes later.
        return None


def precision_at_k(retrieved_ids: List[str], relevant_ids: List[str], k: int) -> float:
    if k <= 0:
        return 0.0
    retrieved_at_k = retrieved_ids[:k]
    hits = sum(1 for cid in retrieved_at_k if cid in relevant_ids)
    return hits / float(k)


def recall(retrieved_ids: List[str], relevant_ids: List[str]) -> float:
    if not relevant_ids:
        return 0.0
    hits = sum(1 for cid in retrieved_ids if cid in relevant_ids)
    return hits / float(len(relevant_ids))


def mean_reciprocal_rank(retrieved_ids: List[str], relevant_ids: List[str]) -> float:
    for idx, cid in enumerate(retrieved_ids):
        if cid in relevant_ids:
            return 1.0 / float(idx + 1)
    return 0.0


class RetrievalEvalHarness:
    """Offline harness to compare dense vs hybrid vs hybrid+rerank profiles."""

    def __init__(self, store: EvalStore) -> None:
        self.store = store
        self.retriever = HybridRetriever()

    def run(self, profile: str = "hybrid") -> list[dict]:
        results = []
        for item in self.store.load():
            response = self.retriever.retrieve(query=item.query, tenant_id=item.tenant_id)
            chunk_ids = [res["chunk_id"] for res in response["results"]]
            metrics = {
                "precision@k": precision_at_k(chunk_ids, item.relevant_chunk_ids, k=len(chunk_ids) or 1),
                "recall": recall(chunk_ids, item.relevant_chunk_ids),
                "mrr": mean_reciprocal_rank(chunk_ids, item.relevant_chunk_ids),
                "profile": profile,
                "retrieved": len(chunk_ids),
            }
            results.append(metrics)
            self.store.write_result(item, metrics)
        return results
