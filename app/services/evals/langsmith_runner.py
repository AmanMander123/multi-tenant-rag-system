from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable, Sequence

from langchain_openai import ChatOpenAI
from langsmith import Client, traceable
from langsmith.evaluation import LangChainStringEvaluator, evaluate

from app.core.config import get_settings
from app.core.secrets import fetch_secret
from app.logger import get_logger
from app.services.retrieval_engine import HybridRetriever

logger = get_logger(__name__)
settings = get_settings()

DATASET_PATH = Path(__file__).with_name("aapl_10k_retrieval_evals_dataset.jsonl")
DATASET_NAME = "aapl-10k-retrieval"
EXPERIMENT_PREFIX = "aapl-10k-rag"


def _load_eval_rows(path: Path) -> list[dict[str, str]]:
    """Load JSONL with `input` and `expected_output` fields."""
    rows: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            if not line.strip():
                continue
            rows.append(json.loads(line))
    return rows


def _get_openai_key() -> str:
    api_key = settings.openai_api_key or fetch_secret(
        settings.openai_secret_name, settings.openai_secret_version
    )
    if not api_key:
        raise RuntimeError("OpenAI API key missing; set OPENAI_API_KEY or Secret Manager.")
    return api_key


def _ensure_dataset(client: Client, rows: Iterable[dict[str, str]]) -> str:
    """Create or update the LangSmith dataset if needed and return its name."""
    try:
        dataset = client.read_dataset(dataset_name=DATASET_NAME)
    except Exception:
        dataset = client.create_dataset(
            dataset_name=DATASET_NAME,
            description="Q&A pairs taken from the Apple 2025 Form 10-K filing.",
        )

    existing_questions = {
        (example.inputs or {}).get("input")
        for example in client.list_examples(dataset_id=dataset.id)
    }
    new_rows = [row for row in rows if row.get("input") not in existing_questions]
    if not new_rows:
        logger.info("Dataset already up to date: %s", DATASET_NAME)
        return dataset.name

    for row in new_rows:
        client.create_example(
            inputs={"input": row["input"]},
            outputs={"expected_output": row["expected_output"]},
            dataset_id=dataset.id,
        )
    logger.info("Added %s new rows to dataset '%s'.", len(new_rows), dataset.name)
    return getattr(dataset, "name", None) or getattr(dataset, "dataset_name", None) or DATASET_NAME


def _make_answer_llm(model: str | None = None) -> ChatOpenAI:
    """LLM used to turn retrieved context into an answer."""
    return ChatOpenAI(
        api_key=_get_openai_key(),
        model=model or settings.retrieval.reranker_model,
        temperature=0,
    )


def _format_context(results: Sequence[dict]) -> str:
    """Render retrieved chunks for prompting."""
    lines = []
    for idx, res in enumerate(results, start=1):
        content = res.get("content", "")
        source = res.get("source_uri") or res.get("document_id")
        lines.append(f"[{idx}] source={source}\n{content}")
    return "\n\n".join(lines)


@traceable(run_type="chain", name="rag_answer")
def _answer_question(question: str, *, tenant_id: str, llm: ChatOpenAI) -> dict:
    retriever = HybridRetriever()
    retrieval = retriever.retrieve(query=question, tenant_id=tenant_id)
    context_text = _format_context(retrieval["results"])

    system_prompt = (
        "You are answering questions using only the provided context. "
        "If the context is missing the answer, say so explicitly. "
        "Keep answers concise (1-3 sentences)."
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": f"Question: {question}\n\nContext:\n{context_text}",
        },
    ]

    response = llm.invoke(messages)
    answer = getattr(response, "content", "") if response else ""

    return {
        "answer": answer,
        "retrieval_results": retrieval["results"],
        "diagnostics": retrieval["diagnostics"],
    }


def run_aapl_10k_eval(
    *,
    limit: int | None = None,
    model: str | None = None,
    tenant_id: str | None = None,
) -> dict:
    """Seed the dataset (if needed) and run the LangSmith eval."""
    rows = _load_eval_rows(DATASET_PATH)
    client = Client()
    dataset_name = _ensure_dataset(client, rows)
    llm = _make_answer_llm(model=model)

    qa_eval = LangChainStringEvaluator(
        "qa",
        config={
            "input_key": "input",
            "prediction_key": "answer",
            "reference_key": "expected_output",
        },
    )

    dataset_data = rows[:limit] if limit else dataset_name

    experiment = evaluate(
        lambda inputs: _answer_question(
            inputs["input"],
            tenant_id=tenant_id or inputs.get("tenant_id") or settings.default_tenant_id,
            llm=llm,
        ),
        data=dataset_data,
        evaluators=[qa_eval],
        experiment_prefix=EXPERIMENT_PREFIX,
        metadata={
            "tenant_id": tenant_id or settings.default_tenant_id,
            "dataset": dataset_name,
            "model": model or settings.retrieval.reranker_model,
        },
        client=client,
        max_concurrency=3,
    )
    return experiment


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run LangSmith evals for the Apple 10-K dataset.")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optionally limit to the first N examples in the dataset.",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Override the OpenAI chat model used to answer questions (default uses reranker_model).",
    )
    parser.add_argument(
        "--tenant-id",
        type=str,
        default=None,
        help="Tenant ID to use for retrieval (defaults to settings.default_tenant_id).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    result = run_aapl_10k_eval(limit=args.limit, model=args.model, tenant_id=args.tenant_id)
    print(f"LangSmith eval run complete. View at: {result.get('url', 'See LangSmith UI')}")
