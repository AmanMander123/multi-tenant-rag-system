# LangSmith Retrieval Evals

Use the Apple 2025 10-K Q&A dataset to score your RAG answers with LangSmith's LLM-as-a-judge evaluators.

## Prerequisites
- Install deps: `uv sync`
- Env vars:
  - `OPENAI_API_KEY` (or configure Secret Manager for `openai_secret_name`/`openai_secret_version`)
- `LANGSMITH_API_KEY` and optionally `LANGSMITH_PROJECT`/`LANGSMITH_ENDPOINT`
- Retrieval stack secrets used by `HybridRetriever` (Pinecone, Postgres) so context can be fetched
- Dataset file already lives at `app/services/evals/aapl_10k_retrieval_evals_dataset.jsonl`.

## Run the eval locally
```bash
uv run python -m app.services.evals.langsmith_runner --limit 25 --model gpt-4o-mini --tenant-id default
```
- The script seeds/updates the LangSmith dataset named `aapl-10k-retrieval`, runs the RAG pipeline to answer each question, and scores answers vs. `expected_output` with the QA evaluator (LLM judge).
- Use `--limit` to smoke-test a subset; omit it to run the full set. `--model` overrides the answering LLM (defaults to `retrieval.reranker_model`).
 - `--tenant-id` lets you override which tenant namespace is used for retrieval.

## Run via API (Cloud Run / GCP)
After deploying the FastAPI service, call the eval endpoint:
```bash
curl -X POST "https://<your-api-host>/api/v1/evals/aapl-10k" \
  -H "Content-Type: application/json" \
  -d '{"limit": 10, "model": "gpt-4o-mini", "tenant_id": "default"}'
```
- The response includes `experiment_url` you can open in LangSmith.
- Ensure the service has `OPENAI_API_KEY`, `LANGSMITH_API_KEY` (and optional `LANGSMITH_PROJECT`/`LANGSMITH_ENDPOINT`), plus retrieval dependencies (Pinecone/Postgres credentials) so context can be fetched during the run.
- The endpoint runs synchronously; large runs can take several minutes. Use `limit` when smoke testing.

## Inspecting in LangSmith
- **Datasets** tab: `aapl-10k-retrieval` shows the seeded Q&A pairs.
- **Projects/Experiments** tab: runs are prefixed with `aapl-10k-rag-*`; drill into a run to see answers, references, QA scores, and the nested retrieval traces (dense/BM25/rerank).
- Toggle `LANGCHAIN_TRACING_V2=true` locally if you want trace-level detail for debugging the pipeline.
