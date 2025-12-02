# LLM-Powered Multi-Tenant RAG System

I am building a multi-tenant, production-ready Retrieval-Augmented Generation (RAG) platform. The mission is to ship a tenant-isolated, cloud-native AI system on Google Cloud that can ingest millions of documents while keeping guardrails, observability, and automation front and center.

---

## Current Build — Data Pipeline + Retrieval + Orchestration ✅
The core data + retrieval + chat pillars are live with nightly reindexing and guardrails. Everything runs against managed GCP services so remaining work is additive (auth, observability, UI).

### Cloud-native ingestion surface
- **FastAPI on Cloud Run** exposes `POST /api/v1/ingestion/documents`, propagates request + tenant context, and optionally enforces Supabase JWT auth (full auth functionality coming soon!).
- **Document validation & logging** ensures each upload receives a `request_id`, `tenant_id`, and structured log context for end-to-end tracing.
- **Cloud Storage fan-in** stores objects inside `gs://{bucket-name}/<tenant>/<date>/<document_id>.pdf` with MIME enforcement and deterministic paths.
- **Pub/Sub fan-out** publishes schema-versioned messages to `projects/{project-id}/topics/ingestion-documents` with attributes for priority, auth subject, and ingestion mode.

### Streaming worker & embeddings
- **Pub/Sub worker** (`app/workers/pubsub_runner.py`) consumes from a dedicated subscription, restores the logging context, and tracks lifecycle status rows inside Supabase Postgres.
- **LangChain pipeline**: `PyPDFLoader` ➜ `RecursiveCharacterTextSplitter` (1k chars / 200 overlap) ➜ OpenAI `text-embedding-3-small`. The API key is fetched at runtime from Secret Manager through ADC.
- **Vector persistence**: embeddings are upserted into Pinecone using per-tenant namespaces, while chunk counts/errors remain in Supabase for dashboards and admin tooling.
- **Operational hardening**: message retries vs. permanent failures, request-scoped temp files, and explicit schema versions keep the worker resilient.

### Automation & drift control
- **Drift detector + queue**: stale or schema/model-mismatched documents are auto-enqueued (`reindex_queue`) with reasons/priorities.
- **Nightly reindex job**: `app/workers/reindex_job.py` replays ingestion (download ➜ chunk ➜ embed ➜ Postgres BM25 ➜ Pinecone) and refreshes audit fields.
- **Cloud Run Job + Scheduler**: Terraform provisions `rag-reindex` and a nightly trigger with OIDC via the CI service account.
- **Visibility & safety rails**: queue attempts/errors, job duration, and processed counts are emitted as structured logs for alerting; per-tenant namespaces keep replays isolated.

### Retrieval engine
- **Hybrid dense + BM25 + rerank** in `app/services/retrieval_engine.py` with tenant namespaces, schema/version tracking, and OpenAI cross-encoder reranking.
- **/api/v1/ask** endpoint runs the hybrid pipeline with LangSmith tracing hooks and structured logging of dense/lexical hits.
- **Config-first tuning**: per-tenant namespace, tsvector config, top-K counts, reranker model/timeout, and schema version captured in `app/core/config.yaml`.

### Evals
- **Offline retrieval metrics harness** (`app/services/evals/retrieval_eval.py`) with Precision@K/Recall/MRR and swappable eval stores.
- **LangSmith runner** (`app/services/evals/langsmith_runner.py`) seeds an Apple 10-K dataset and runs LLM-as-judge evals end-to-end for RAG answers.

### Orchestration + guardrails
- **Chat orchestrator** (`app/services/orchestrator.py`) wires guardrails → hybrid retrieval → prompt render → OpenAI with model fallbacks; supports streaming.
- **Prompt registry** (`app/services/prompt_registry.py`) loads versioned YAML templates (`prompts/default.yml`) with overridable name/version per request.
- **Guardrails** (`app/services/guardrails.py`) enforce input size limits, prompt-injection heuristics, PII redaction in/out, and history summarization.
- **/api/v1/chat** endpoint returns full JSON responses or text streams, carrying `tenant_id` from Supabase auth middleware.

### Security & observability groundwork
- **SupabaseAuthMiddleware** is already wired in so JWT claims, quotas, and tenant IDs flow through the stack before a UI even exists.
- **Structured logging everywhere**: the logger carries metadata across API ➜ Pub/Sub ➜ worker, paving the way for Cloud Logging dashboards, OpenTelemetry traces, and Slack/Pager alerts.
- **Principle of least privilege**: Cloud Run, Pub/Sub, Storage, Pinecone, Supabase, and Secret Manager interactions are isolated per role; no sensitive values live in git.

### Remaining near-term tasks
- Expose an admin API/CLI to manually enqueue reindex work (beyond drift auto-detection).
- Add log-based metrics + alerts for reindex queue depth, retrieval latency, and chat guardrail blocks.
- Enforce auth + quotas on `/ask` and `/chat`, add rate limits, and surface idempotency keys.
- Instrument OpenTelemetry spans across ingestion → retrieval → chat; publish hit@K, rerank timing, and LLM latency to dashboards/alerts.
- Add UI/Admin surfaces for chat + ingestion dashboards with session history and eval feedback.

---

## Roadmap — Next Pillars
### Retrieval Quality Loop
- Gate deploys with eval runs (Precision@K/Recall/MRR, cost/latency) and tenant-specific blend/rerank tuning.
- Add runtime retrieval metrics + dashboards; experiment with semantic caches and rerank alternatives.

### Tenant-Grade Access & Limits
- Enforce auth on `/ask` + `/chat`, issue API keys/JWT validation, and add quotas/rate limits per tenant.
- Idempotency keys, request-size guards, DLQ processing, and noisy-tenant protections.

### Observability First
- End-to-end OpenTelemetry spans (ingestion → retrieval → chat), structured logs, and metrics for hit@K, rerank timing, cache hit rate, and LLM latency.
- SLO-driven alerts to Slack/PagerDuty; LangSmith tracing rolled into dashboards.

### Tenant-Facing UI/Admin
- React/Vite chat + ingestion dashboard with session history, feedback loops, and eval visibility backed by the existing APIs.

---

## High-Level Architecture
Every milestone traces back to this blueprint so progress is easy to follow.

![High-Level Architecture](designs/high_level_architecture.svg)

---

## Local Development Runbook
Even though the stack targets managed services, you can iterate locally with the same primitives.

1. **Install deps**
   ```bash
   uv sync
   ```
2. **Authenticate & export env vars**
   ```bash
   gcloud auth application-default login
   export PUBSUB_SUB_INGEST_WORKER="projects/<project>/subscriptions/<subscription>"
   export SUPABASE_DB_URL="postgresql://<user>@<host>:6543/postgres"
   export SUPABASE_DB_PASSWORD="<supabase password>"
   export PINECONE_API_KEY="<pinecone key>"
   ```
3. **Run the API**
   ```bash
   uv run uvicorn app.main:app --reload
   ```
4. **Run the Pub/Sub worker**
   ```bash
   uv run python -m app.workers.pubsub_runner
   ```
5. **Smoke test ingestion**
   ```bash
   curl -X POST "http://localhost:8000/api/v1/ingestion/documents" \
     -H "x-tenant-id: demo" \
     -F "file=@sample.pdf;type=application/pdf"
   ```
   The API uploads to Cloud Storage, publishes a Pub/Sub message, and the worker logs chunk + Pinecone upsert counts.

6. **Ask a question through chat orchestration**
   ```bash
   curl -X POST "http://localhost:8000/api/v1/chat" \
     -H "Content-Type: application/json" \
     -d '{
       "message": "Summarize the SLA obligations.",
       "prompt_version": "2024-10-01",
       "stream": false
     }'
   ```
   The response includes the chosen model, prompt metadata, retrieved context, and answer. Set `"stream": true` to receive a text stream instead.

7. **Run retrieval or RAG evals**
   - Offline retrieval metrics: use `RetrievalEvalHarness` with your qrels (see `app/services/evals/retrieval_eval.py`).
   - LangSmith RAG eval: `uv run python app/services/evals/langsmith_runner.py --limit 5` (requires LangSmith API key/env).

> Tip: assign yourself a Dev subscription in GCP so you can watch backlog depth and autoscaling behavior during local tests.

---

## Follow Along
I am sharing regular build notes, Terraform drops, retrieval experiments, and now eval/orchestration progress on GitHub and on X. Next up: wire auth/quotas, observability dashboards, and the tenant-facing UI.
