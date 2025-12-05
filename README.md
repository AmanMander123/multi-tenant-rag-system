# LLM-Powered Multi-Tenant RAG System

A production-minded RAG platform built on Google Cloud. It ingests tenant-scoped PDFs, embeds them with LangChain + OpenAI, stores dense vectors in Pinecone alongside BM25-ready text in Supabase Postgres, and serves hybrid retrieval plus chat orchestration behind FastAPI on Cloud Run.

---

## What’s inside
- **API surface (FastAPI on Cloud Run)**: `POST /api/v1/ingestion/documents` uploads PDFs to Cloud Storage and publishes schema-versioned Pub/Sub jobs; `/api/v1/ask` runs hybrid retrieval; `/api/v1/chat` orchestrates guardrails → retrieval → prompts → OpenAI with streaming; `/api/v1/evals/aapl-10k` triggers LangSmith RAG evals.
- **Ingestion worker**: streaming Pub/Sub consumer (`app/workers/pubsub_runner.py`) downloads from GCS, chunks via LangChain (`RecursiveCharacterTextSplitter`), embeds with OpenAI `text-embedding-3-small`, writes chunk metadata + BM25 vectors to Supabase Postgres, and upserts tenant-isolated vectors into Pinecone.
- **Reindex + drift control**: nightly job (`app/workers/reindex_job.py`) detects stale/schema-mismatched docs, fills a Supabase-backed `reindex_queue`, and replays ingestion (GCS ➜ chunk ➜ embed ➜ Postgres ➜ Pinecone). Push-compatible worker (`app/workers/push_app.py`) exists for Pub/Sub push delivery.
- **Retrieval & chat**: hybrid dense + lexical search (`app/services/retrieval_engine.py`) with OpenAI cross-encoder style reranker, LangSmith tracing, and blend diagnostics. Chat orchestration (`app/services/orchestrator.py`) adds guardrails (PII redaction + prompt-injection heuristics), prompt registry (`prompts/default.yml`), history summarization, model fallbacks, and optional streaming responses.
- **Evaluations**: offline retrieval harness with Precision@K/Recall/MRR (`app/services/evals/retrieval_eval.py`) and a LangSmith Apple 10-K dataset runner (`app/services/evals/langsmith_runner.py`, also exposed via the API) that seeds/updates the dataset before running LLM-as-judge evals.
- **Configuration & security**: `app/core/config.yaml` drives defaults with env overrides; SupabaseAuthMiddleware performs best-effort JWT validation + tenant resolution; secrets are pulled from GCP Secret Manager via ADC. Structured logging threads request/tenant IDs through API, Pub/Sub, worker, and reindex flows.
- **Infra-as-code**: Terraform provisions Cloud Run services/jobs (API, worker, reindex), Pub/Sub topics/subscription + DLQ, upload/temp buckets with IAM, Secret Manager bindings, GitHub Actions Workload Identity Federation, Cloud Scheduler trigger for nightly reindex, BigQuery log sink, and alerting on backlog/DLQ.

---

## API quickstart
- **Ingest**: `POST /api/v1/ingestion/documents` with multipart `file` (PDF enforced) + optional bearer token (Supabase JWT) and `x-tenant-id`. Files land in `gs://<bucket>/<tenant>/<date>/<document_id>-<filename>`.
- **Retrieve**: `POST /api/v1/ask` with `{ "query": "..." }` returns blended + reranked chunks plus diagnostics (dense/lexical counts, merged candidates, returned).
- **Chat**: `POST /api/v1/chat` with `{ "message": "...", "history": [], "prompt_version": "2024-10-01", "stream": false }` returns answer + retrieval context; set `stream: true` for text streaming.
- **Run eval**: `POST /api/v1/evals/aapl-10k` to kick off LangSmith evals against the bundled dataset (blocking call; returns experiment metadata/URL).

---

## Local development
1. **Install deps**
   ```bash
   uv sync
   ```
2. **Set credentials/config**
   ```bash
   gcloud auth application-default login  # for Secret Manager + GCS locally
   export OPENAI_API_KEY="<openai>"
   export PINECONE_API_KEY="<pinecone>"
   export SUPABASE_DB_URL="postgresql://<user>@<host>:6543/postgres"
   export SUPABASE_DB_PASSWORD="<password>"
   export PUBSUB_TOPIC_INGEST="projects/<project>/topics/<topic>"
   export PUBSUB_SUB_INGEST_WORKER="projects/<project>/subscriptions/<sub>"
   export GCS_UPLOAD_BUCKET="<bucket>"
   ```
3. **Run the API**
   ```bash
   uv run uvicorn app.main:app --reload
   ```
4. **Run the ingestion worker (pulls from Pub/Sub)**
   ```bash
   uv run python -m app.workers.pubsub_runner
   ```
5. **Smoke test ingestion**
   ```bash
   curl -X POST "http://localhost:8000/api/v1/ingestion/documents" \
     -H "x-tenant-id: demo" \
     -F "file=@sample.pdf;type=application/pdf"
   ```
6. **Chat through orchestration**
   ```bash
   curl -X POST "http://localhost:8000/api/v1/chat" \
     -H "Content-Type: application/json" \
     -d '{"message": "Summarize the SLA obligations.", "prompt_version": "2024-10-01", "stream": false}'
   ```
7. **Reindex or evals (optional)**
   ```bash
   uv run python -m app.workers.reindex_job --dry-run           # see queued/stale docs
   uv run python app/services/evals/langsmith_runner.py --limit 5
   ```

---

## Configuration notes
- Base settings live in `app/core/config.yaml`; environment variables override values (nested fields via `__`, e.g., `RETRIEVAL__RERANK_TOP_K=6`). `APP_ENV` is respected by the config loader.
- Guardrails, prompt defaults, retrieval blend parameters, and schema versions are all configuration-driven so per-tenant tuning can be added without code changes.
- Logging can be emitted as JSON for Cloud Logging (`APP_LOG_JSON=1`) and carries request/tenant/document IDs for tracing through API → Pub/Sub → worker → reindex.

---

## Deployment
- Container images are built from `Dockerfile.api` and `Dockerfile.worker`.
- Terraform in `infra/terraform` stands up Cloud Run (public API + internal worker/push endpoint + reindex job), Pub/Sub topics/subscription with DLQ, upload/temp buckets, Secret Manager bindings (OpenAI, Pinecone, Supabase, LangSmith), Cloud Scheduler nightly trigger for the reindex job, GitHub Actions Workload Identity Federation, and monitoring policies for backlog/DLQ.
- Pinecone uses per-tenant namespaces; Supabase Postgres stores documents/chunks/reindex queue and powers BM25 search; Cloud Storage holds raw uploads; GCP Secret Manager supplies runtime credentials.
