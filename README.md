# LLM-Powered Multi-Tenant RAG System

I am building a multi-tenant, production-ready Retrieval-Augmented Generation (RAG) platform. The mission is to ship a tenant-isolated, cloud-native AI system on Google Cloud that can ingest millions of documents while keeping guardrails, observability, and automation front and center.

---

## Current Build — Data Pipeline + Automation/Drift ✅
The Data Pipeline pillar now includes nightly reindexing and drift control. Every component runs against managed GCP services so the remaining pillars can be layered in quickly.

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

### Security & observability groundwork
- **SupabaseAuthMiddleware** is already wired in so JWT claims, quotas, and tenant IDs flow through the stack before a UI even exists.
- **Structured logging everywhere**: the logger carries metadata across API ➜ Pub/Sub ➜ worker, paving the way for Cloud Logging dashboards, OpenTelemetry traces, and Slack/Pager alerts.
- **Principle of least privilege**: Cloud Run, Pub/Sub, Storage, Pinecone, Supabase, and Secret Manager interactions are isolated per role; no sensitive values live in git.

### Remaining Data Pipeline tasks
- Expose an admin API/CLI to manually enqueue reindex work (beyond drift auto-detection).
- Add log-based metrics + alerts for reindex queue depth and success/failure rates.
- Harden cold storage lifecycle policies and retention for uploaded PDFs.

---

## Roadmap — Next Pillars
### Retrieval Quality Loop
- Stand up eval datasets + harness (Precision@K/Recall/MRR, latency/cost) that gate deploys.
- Pipe eval + runtime retrieval metrics into dashboards and refine reranker blend weights per tenant.

### Tenant-Grade Access & Limits
- Enforce auth on `/ask` + ingestion, issue API keys/JWT validation, and add quotas/rate limits.
- Add idempotency keys, request-size guards, and noisy-tenant protections.

### LLM Orchestrator & Guardrails
- Introduce a model router for quality/cost-aware selection, streaming, and tool/function calling.
- YAML prompt templates with versioning/canary/rollback plus safety/PII filters and structured request/response validation.

### Observability First
- Instrument OpenTelemetry spans across ingestion → retrieval → rerank; add metrics for hit@K, rerank latency, and store errors.
- Wire alerting policies to SLOs (Cloud Monitoring/LangSmith/Slack/PagerDuty).

### Tenant-Facing UI/Admin
- Build a thin chat + ingestion dashboard (React/Vite) with session history and eval feedback loops backed by the existing APIs.

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

> Tip: assign yourself a Dev subscription in GCP so you can watch backlog depth and autoscaling behavior during local tests.

---

## Follow Along
I am sharing regular build notes, Terraform drops, and retrieval experiments on GitHub and on X. Next up: bring the Retrieval Engine online, wire up prompt/guardrail scaffolding, and add UI + observability layers. If you want to see what a production-grade AI platform work looks like in the open, follow along and say hi.
