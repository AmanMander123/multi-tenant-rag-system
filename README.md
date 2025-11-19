# LLM-Powered Multi-Tenant RAG System

I am building a multi-tenant, production-ready Retrieval-Augmented Generation (RAG) platform. The mission is to prove how to ship a tenant-isolated, cloud-native AI system on Google Cloud that can ingest millions of documents while keeping guardrails, observability, and automation front and center.

---

## Current Build — Data Pipeline ✅
The Data Pipeline pillar from the architecture is almost complete (Automation & Drift is still on deck). Every component now runs against managed GCP services so the remaining pillars can be layered in quickly.

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

### Security & observability groundwork
- **SupabaseAuthMiddleware** is already wired in so JWT claims, quotas, and tenant IDs flow through the stack before a UI even exists.
- **Structured logging everywhere**: the logger carries metadata across API ➜ Pub/Sub ➜ worker, paving the way for Cloud Logging dashboards, OpenTelemetry traces, and Slack/Pager alerts.
- **Principle of least privilege**: Cloud Run, Pub/Sub, Storage, Pinecone, Supabase, and Secret Manager interactions are isolated per role; no sensitive values live in git.

### Remaining Data Pipeline tasks
- Nightly ETL / delta re-index workflows.
- Retriever drift monitoring and Slack alerts when embeddings skew.

---

## Roadmap — Retrieval ➜ LLM Core ➜ Backend ➜ Frontend
I am tackling the rest of the architecture column by column so followers can see the system evolve.

### Retrieval Engine
- Launch the hybrid retriever: Pinecone dense vectors + BM25 (likely Firestore/Elastic) with a cross-encoder reranker.
- Automate chunk upserts/backfills so reprocessing stays idempotent across tenants.
- Ship evaluation harnesses (Precision@K, Recall, MRR) plus latency/cost telemetry per query profile.

### LLM Core
- Build an orchestrator that routes to the best LLM per request (quality vs. cost) and streams/tool-calls responses.
- Introduce YAML-driven prompt management with versioning, canary/AB toggles, and rollbacks.
- Add guardrails (PII and safety filters, jailbreak defenses) plus semantic + response caches to cut costs.

### Backend Platform & Ops
- Expand the FastAPI surface (`/ask`, `/index`, `/eval`, `/admin`) with pagination, streaming, rate limits, and idempotency keys.
- Harden platform controls: API keys, quotas, tenant isolation, secrets service, and audit trails.
- Terraform everything (Cloud Run, Pub/Sub, Storage, Firestore, Secret Manager) and ship GitHub Actions ➜ Cloud Run blue/green deploys.

### Frontend & Observability
- Ship a Vite/React chat workspace with tenant-aware history, admin dashboards, and ingestion telemetry.
- Observability stack: OpenTelemetry traces, LangSmith spans, structured logs, custom metrics, Slack/Pager alerts tied to SLOs.
- Feedback & eval loops: thumbs up/down into Supabase, Ragas/custom eval harness, and win-rate dashboards.

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
