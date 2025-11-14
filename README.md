# LLM-Powered Multi-Tenant RAG System

I am building a production-ready Retrieval-Augmented Generation (RAG) platform. The focus is tenant isolation, security, and the ability to scale ingestion + retrieval to millions of documents on Google Cloud.

---

## Project Status — What’s Done
- **FastAPI ingestion surface** with `POST /api/v1/ingestion/documents`, request correlation IDs, and structured logging.
- **Celery-based async pipeline** (for local/dev) backed by Redis, feeding all logs/context into the worker tier.
- **LangChain document pipeline**: PyPDF loader ➜ RecursiveCharacterTextSplitter (1 000 chars / 200 overlap) ➜ OpenAI `text-embedding-3-small` embeddings.
- **Secret hygiene**: OpenAI key lives in GCP Secret Manager, loaded via IAM-secured ADC; no credentials in the repo.
- **Telemetry-ready logging**: custom logger propagates request/tenant metadata across API and worker boundaries for later Cloud Logging / OpenTelemetry wiring.
- **Security defaults**: `.env`, `.venv/`, and other sensitive files are git-ignored; only config templates ship in git.

## Roadmap — What’s Next
1. **GCP-native fan-out**: split API + worker into separate Cloud Run services using Pub/Sub instead of Celery/Redis; autoscale by subscription lag.
2. **Storage tier**: persist uploads in Cloud Storage and write chunk metadata + embeddings to a managed vector DB.
3. **Tenant isolation**: per-tenant namespaces, quotas, and idempotency keys enforced at the API layer and reflected in storage.
4. **Retrieval & guardrails**: add retrievers, evaluation harnesses, caching, and policy enforcement for live traffic.
5. **IaC + CI/CD**: Terraform-managed infra

---

## Architecture Snapshot
- **Ingestion API (Cloud Run)**: validates PDFs, persists uploads, publishes async jobs, and returns immediate acknowledgements.
- **Worker Service**: currently Celery for local dev; will become its own Cloud Run deployment consuming Pub/Sub, running LangChain chunking + embeddings, and writing to storage.
- **Secrets**: Google Secret Manager supplies the OpenAI key; services access it via IAM/ADC so nothing sensitive lands in env vars or git.
- **Logging & context**: request IDs, tenant metadata, and filenames flow through API ➜ queue ➜ worker for full-trace observability.

---

## Local Development Runbook
1. **Install deps**
   ```bash
   uv sync
   ```
2. **Authenticate for Secret Manager**
   ```bash
   gcloud auth application-default login
   ```
3. **Environment config** (`.env` is git-ignored)
   ```
   CELERY__BROKER_URL=redis://localhost:6379/0
   CELERY__RESULT_BACKEND=redis://localhost:6379/1
   ```
4. **Infra dependencies**
   ```bash
   docker run --rm -p 6379:6379 redis:7
   ```
5. **Run services**
   ```bash
   uv run uvicorn app.main:app --reload
   uv run celery -A app.core.celery_app.celery_app worker --loglevel=info --pool=solo
   ```
6. **Smoke test**
   ```bash
   curl -X POST "http://localhost:8000/api/v1/ingestion/documents" \
     -F "file=@sample.pdf;type=application/pdf"
   ```
   The worker terminal prints `[INGESTION TEST] filename=... chunks=... model=...` to confirm embeddings ran.

> **Why Celery?** It keeps local iteration fast. Production will swap in Cloud Run services + Pub/Sub for durable, horizontally scalable fan-out.

---

## Async Ingestion Flow (Current)
1. Client uploads PDF.
2. API validates MIME type, persists the file to a temp path, and logs context (request_id, tenant, filename).
3. Job payload (filename, context, path) is enqueued via Celery/Redis.
4. Worker restores the logging context, loads the PDF with LangChain, chunks, and embeds via OpenAI.
5. Diagnostic log + print confirm chunk counts; the next milestone will persist vectors to storage.

---

## Future Cloud Deployment Strategy
- **Separate Cloud Run services** for API and worker, each with its own service account, autoscaling policy, and IAM role boundaries.
- **Pub/Sub (or Cloud Tasks)** replaces Redis so ingestion can spike to millions of jobs with durable retries and dead-letter queues.
- **Cloud Storage** for uploads, **managed vector DB** for embeddings, and **Cloud SQL/Firestore** for metadata + job states.
- **Cloud Monitoring + Logging** dashboards for queue depth, worker throughput, and per-tenant SLAs.

---

## System Building Blocks
- **Data sources**: PDFs today, roadmap includes web, APIs, and database connectors.
- **ETL & embeddings**: LangChain pipelines with configurable chunk size/overlap and swappable embedding providers.
- **Storage**: object store, vector DB, metadata DB, and nightly drift monitors.
- **Retrieval engine**: hybrid BM25 + dense + rerankers, evaluation metrics (P@K/Recall/MRR), and caching layers.
- **LLM core**: prompt mgmt, tool calling, guardrails, semantic + response caches (to be implemented).
- **Ops**: Terraform/IaC, GitHub Actions, Cloud Run blue/green, feature flags.

---

## Security & Observability Principles
- Secrets stay in Secret Manager; `.env` and other sensitive files are ignored via `.gitignore`.
- Structured logging ensures every log line includes correlation IDs and (future) tenant identifiers.
- Planned guardrails: rate limiting, JWT/API key auth, Workload Identity, and per-tenant quotas once the control plane is wired up.

---

## API Usage
- **Endpoint**: `POST /api/v1/ingestion/documents`
- **Body**: `multipart/form-data` with a `file` field (PDF only for now).
- **Response**: 200 JSON acknowledgement detailing filename + status while the worker handles chunking/embedding asynchronously.

Example:
```bash
curl -X POST "http://localhost:8000/api/v1/ingestion/documents" \
  -F "file=@sample.pdf;type=application/pdf"
```

---

## Follow Along
I’m sharing frequent build notes and architecture breakdowns on GitHub and X. The next drop will cover the Cloud Run + Pub/Sub refactor, vector persistence, and tenant-aware controls. Stay tuned if you’re interested in how to design AI systems that are secure, fault tolerant, and ready for millions of users.
