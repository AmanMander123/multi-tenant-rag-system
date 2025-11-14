# Cloud Run + Pub/Sub Deployment Plan

This document freezes the contract for the first cloud release: container artifacts, Pub/Sub schemas, secrets, IAM, and observability expectations. The goal is to make Terraform and CI/CD work deterministic once this plan is accepted.

---

## 1. Scope & Assumptions
- **Services covered**: ingestion API (`app.main:app`) and async worker (LangChain chunking + embeddings).
- **Cloud**: Single Google Cloud project `virtual-assistant-460209`, prod-only footprint (no separate staging).
- **Runtime**: Cloud Run fully managed, minimum CPU always off (cost-optimized). Worker can switch to CPU always-on once throughput justifies it.
- **Queue**: Pub/Sub standard topics + subscriptions; no Redis/Celery in production path.
- **Auth**: Supabase backs user signup/login and issues JWTs; API ships with stub middleware today but infra reserves config/secrets so enforcement can be toggled on without redesign.
- **Secrets**: Google Secret Manager + Workload Identity Federation; no static keys in env vars.

---

## 2. Container Artifacts & Build Contracts

| Component | Decision | Reasoning | Status |
| --- | --- | --- | --- |
| Base Image | `gcr.io/distroless/python3-debian12:nonroot` | Distroless cuts CVE surface; using the nonroot tag keeps the image reproducible without chasing digest pins. | Locked |
| Builder image | `python:3.11-slim` | Need build stage with `uv`; slim keeps toolchain small. | Locked |
| Runtime user | Non-root UID 65532 | Cloud Run best practice; required for some org policies. | Locked |
| Entrypoint (API) | `uv run gunicorn app.main:app -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8080 --workers=2` | Gunicorn fronting Uvicorn gives graceful shutdown + concurrency, matches Cloud Run’s 80 concurrent limit. | Locked (revisit after load test) |
| Entrypoint (Worker) | `uv run python -m app.workers.pubsub_runner` | Dedicated runner module implements Pub/Sub pull, ack/nack, and structured logging context restore. | Locked |
| Artifact naming | `gcr.io/virtual-assistant-460209/rag-api:${GIT_SHA}` and `gcr.io/virtual-assistant-460209/rag-worker:${GIT_SHA}` | Tagging with commit SHA makes rollbacks reproducible; single project keeps registry simple. | Locked |

All container decisions above are final for the `virtual-assistant-460209` project; there is one prod environment only.

### Cloud Run Runtime Settings

| Service | Min Instances | Max Instances | CPU / Memory | Concurrency | Timeout |
| --- | --- | --- | --- | --- | --- |
| Ingestion API | 0 | 2 | 1 vCPU / 512 MiB | 80 | 300 s |
| Worker | 0 | 4 | 1 vCPU / 1 GiB | 1 | 600 s |

Autoscaling will later be driven by Pub/Sub backlog metrics, but these starting limits keep baseline costs low while allowing burst headroom.

---

## 3. Runtime Configuration Matrix

| Variable | API Value | Worker Value | Source | Rationale |
| --- | --- | --- | --- | --- |
| `APP_ENV` | `prod` | `prod` | Plain env in Terraform | Single prod environment; keeps runtime logic simple. |
| `GCLOUD_PROJECT` | `virtual-assistant-460209` | `virtual-assistant-460209` | Terraform set | Required for telemetry SDKs and Secret Manager auto-config. |
| `PUBSUB_TOPIC_INGEST` | `projects/virtual-assistant-460209/topics/ingestion-documents` | — | ConfigMap-style env | API publishes using this topic. |
| `PUBSUB_SUB_INGEST_WORKER` | — | `projects/virtual-assistant-460209/subscriptions/ingestion-worker-sub` | Terraform | Worker identifies subscription to pull from. |
| `GCS_UPLOAD_BUCKET` | `gs://va-rag-uploads-prod` | `gs://va-rag-uploads-prod` | Terraform | Single source of truth for document blobs. |
| `FIRESTORE_COLLECTION_NAMESPACE` | `tenants` | `tenants` | Plain env | Firestore stores non-relational tenant/profile data. |
| `PINECONE_INDEX_URL` | — | `https://rag-embeddings-prod-gcp-1a.pinecone.io` | Secret Manager | Worker upserts/query embeddings through Pinecone. |
| `OPENAI_API_KEY` | resolved via Secret Manager | resolved via Secret Manager | Secret + Workload Identity | Keeps keys out of env. |
| `SUPABASE_URL` | `https://virtualassistant460209.supabase.co` | — | Plain env | Required once the API calls Supabase REST endpoints and relational DB. |
| `SUPABASE_JWKS_URL` | `https://virtualassistant460209.supabase.co/auth/v1/jwks` | — | Plain env | Allows JWT verification without redeploying infra. |
| `SUPABASE_JWT_AUDIENCE` | `auth.virtualassistant460209.supabase.co` | — | Secret Manager | Controls which tokens are valid; keeps project ref out of git. |
| `SUPABASE_DB_URL` | `postgresql://supabase_user@virtualassistant460209.supabase.co:6543/postgres` | `postgresql://supabase_service@virtualassistant460209.supabase.co:6543/postgres` | Secret Manager | API writes metadata, worker updates status via Supabase Postgres.

All runtime configuration values above are committed for the single prod environment within `virtual-assistant-460209`.

---

## 4. Pub/Sub Message Contract

```json
{
  "version": "2024-09-15",
  "request_id": "uuid4",
  "tenant_id": "slug",
  "document_id": "uuid4",
  "filename": "original.pdf",
  "gcs_uri": "gs://va-rag-uploads-prod/tenant-a/2024/09/14/original.pdf",
  "content_type": "application/pdf",
  "chunk_config": {
    "size": 1000,
    "overlap": 200
  },
  "submitted_at": "2024-09-15T12:34:56Z",
  "attributes": {
    "source": "api-v1",
    "ingestion_mode": "standard"
  }
}
```

- **version**: semantic marker so workers can reject unsupported payloads.
- **request_id / tenant_id / document_id**: enforce idempotency and tenant isolation. `tenant_id` must match the Supabase tenant/org claim so auth and storage stay consistent. `document_id` is generated API-side to align storage + metadata rows.
- **filename / content_type**: immutable metadata; validated in API before publishing.
- **gcs_uri**: API uploads file to Cloud Storage before publish; workers never receive raw bytes.
- **chunk_config**: API copies the config used during validation (can be per-tenant). Worker treats it as authoritative.
- **submitted_at**: used for SLA tracking and Pub/Sub dead-letter decisions.
- **attributes**: open map for future controls (eg. retriable flag). Keep small to avoid hitting Pub/Sub attribute limits.

**Pub/Sub Attributes (message-level)**  
`tenant_id`, `request_id`, `priority` (`standard|high`), `schema_version`, `encryption_key_id`, `auth_subject`. `auth_subject` carries the Supabase JWT `sub` for audit trails. Attributes allow filtering and IAM Conditions later.

**Ack/Retry Policy**  
- Worker must `nack` on transient errors; Pub/Sub handles exponential backoff.  
- Subscription uses max delivery attempts = 5 and maximum delivery age = 900 seconds; beyond that the message routes to DLQ topic `projects/virtual-assistant-460209/topics/ingestion-documents-dlq`.  
- Dead-letter includes original payload and error reason attribute `failure_code`.

Retry parameters above are encoded directly in Terraform (`dead_letter_policy` + `expiration_policy`), so queue behavior is deterministic.

---

## 5. Storage & Data Path

| Asset | Purpose | Structure | Notes |
| --- | --- | --- | --- |
| `gs://va-rag-uploads-prod` | Raw uploads | `tenant_id/YYYY/MM/DD/${document_id}.pdf` | Lifecycle rule deletes objects 30 days after `status=complete` flag is written. |
| `gs://va-rag-worker-temp-prod` | Worker temp scratch | `document_id/chunk-*.json` | 7-day lifecycle; only Cloud Run worker SA has read/write. |
| Pinecone index `rag-embeddings-prod` | Stores embeddings + metadata refs | Namespace = `tenant_id`, vector dim 1 536, cosine distance | Hosted in Pinecone `gcp-starter` environment; worker upserts batches via REST. |
| Supabase Postgres (`virtualassistant460209`) | Tracks ingestion states + relational metadata | Schema `public.documents(document_id uuid primary key, tenant_id text, status text, gcs_uri text, submitted_at timestamptz)` | Supabase handles backups + high availability; API + worker share the same DB. |
| Firestore collection `tenants` | Non-relational tenant configs / quotas | Document key = `tenant_id`, fields for plan/quota/auth | Gives flexible metadata store for UI + control plane.

---

## 6. Secrets & Key Management

| Secret | Source | Accessed By | Injection Mechanism | Reasoning |
| --- | --- | --- | --- | --- |
| OpenAI API Key | Secret Manager `projects/virtual-assistant-460209/secrets/openai-api-key` | Worker only | `gcloud secrets versions access` via ADC | Only worker needs embedding access; API stays principle of least privilege. |
| Pinecone API Key | Secret Manager `projects/virtual-assistant-460209/secrets/pinecone-api-key` | Worker | Env var `PINECONE_API_KEY` | Required for embedding upserts/queries. |
| Supabase JWT Audience | Secret Manager `projects/virtual-assistant-460209/secrets/supabase-jwt-aud` | API | Plain env sourced from Secret Manager | Keeps project-specific IDs outside of git history. |
| Supabase Service Role Key (optional) | Secret Manager `projects/virtual-assistant-460209/secrets/supabase-service-role-key` | API (disabled by default) | Mounted file | Only needed if API must call Supabase admin APIs; remains disabled until UI launches. |
| Supabase DB Password | Secret Manager `projects/virtual-assistant-460209/secrets/supabase-db-password` | API + worker | Env var `SUPABASE_DB_PASSWORD` | Enables secure connections to Supabase Postgres. |
| JWT Signing Key (future) | Secret Manager `projects/virtual-assistant-460209/secrets/first-party-jwt-key` | API | Mount via Secret Manager FS (beta) | Needed once you issue first-party tokens (eg. internal service-to-service). |

Adopt CMEK for storage buckets if compliance demands it; specify `kms_key_ring` + `kms_crypto_key` names now if so.

---

## 7. IAM & Identity Map

| Identity | Roles | Rationale |
| --- | --- | --- |
| `svc-rag-api@virtual-assistant-460209.iam.gserviceaccount.com` | `roles/run.invoker`, `roles/pubsub.publisher` (topic condition), `roles/storage.objectCreator` on `va-rag-uploads-prod`, `roles/secretmanager.secretAccessor` (Supabase/Pinecone secrets only) | API must upload to GCS, publish messages, and read Supabase config. |
| `svc-rag-worker@virtual-assistant-460209.iam.gserviceaccount.com` | `roles/pubsub.subscriber`, `roles/storage.objectViewer` on `va-rag-uploads-prod`, `roles/secretmanager.secretAccessor` | Worker consumes messages, reads blobs, and calls Pinecone/OpenAI. |
| `svc-ci@virtual-assistant-460209.iam.gserviceaccount.com` | `roles/artifactregistry.writer`, `roles/run.admin`, `roles/iam.serviceAccountUser` (impersonate API/worker SAs), `roles/pubsub.admin` (topics/subscriptions) | GitHub Actions deploy identity pushes images and updates Cloud Run/infra. |

Add IAM Conditions tying `pubsub.publisher` to specific topic resource to limit blast radius. If Workload Identity Federation is used for GitHub, map OIDC audience + repo.

Buckets remain project-private; tenants only access data through the API/worker path, keeping IAM surface minimal.

---

## 8. Networking, Security, Observability

- **Networking**: services stay publicly reachable for prod; Cloud Armor policy `va-prod-default` (allow all) is attached for later tightening. No VPC connector needed yet.
- **Ingress**: API currently allows unauthenticated traffic; Supabase JWT middleware flag flips once UI is live. Worker remains internal-only (no ingress).
- **Egress controls**: API permitted to reach Supabase + Cloud Storage; worker allowed to reach GCS, Pub/Sub, `*.openai.com`, and `*.pinecone.io` via default egress.
- **Logging**: Structured JSON logs with `request_id`, `tenant_id`, `auth_subject`. Cloud Logging sink `va-prod-traces` routes to BigQuery for analytics.
- **Metrics**: custom metrics `custom.googleapis.com/ingestion_enqueue_latency_seconds` and `custom.googleapis.com/worker_processing_latency_seconds` emitted via OpenTelemetry exporter.
- **Alerting/SLOs**: SLO1 = 99% of documents processed end-to-end within 5 minutes; SLO2 = Pub/Sub DLQ count stays below five messages over any 10-minute window. Alert policies fire when backlog age > 120 seconds or DLQ count >= 5.

---

## 9. Terraform Work Breakdown

1. **Artifact Registry** module (if not already) for storing images.
2. **Secret Manager** entries and IAM bindings.
3. **Cloud Storage** buckets + lifecycle + CMEK bindings.
4. **Pub/Sub** topics, subscriptions, DLQ wiring, IAM.
5. **Cloud Run** services (API + worker) with env vars, service accounts, min/max instances, concurrency.
6. **Monitoring**: log-based metrics, alert policies.
7. **Workload Identity Federation**: provider `projects/virtual-assistant-460209/locations/global/workloadIdentityPools/github/providers/virtual-assistant` mapping GitHub OIDC to `svc-ci`.
8. **Optional**: VPC connector, Cloud Armor tightening when auth goes live.

Terraform modules will expect all inputs listed above to be finalized; otherwise count on follow-up migrations.

---

## 10. Decision Summary

All configuration choices for the prod environment in `virtual-assistant-460209` are locked:

- **Projects**: single GCP project `virtual-assistant-460209`.
- **Buckets**: `va-rag-uploads-prod`, `va-rag-worker-temp-prod` with lifecycle + CMEK-ready policies.
- **Data stores**: Pinecone index `rag-embeddings-prod`, Supabase Postgres (`virtualassistant460209`), Firestore collection `tenants`.
- **Queueing**: Pub/Sub topic `ingestion-documents`, subscription `ingestion-worker-sub`, DLQ `ingestion-documents-dlq`.
- **Compute sizing**: API (0–2 instances, 1 vCPU, 512 MiB, concurrency 80); Worker (0–4 instances, 1 vCPU, 1 GiB, concurrency 1).
- **Security & identity**: Service accounts and Secret Manager entries defined above; Supabase + Pinecone secrets scoped per service.
- **Observability**: Metrics + SLOs enumerated; alert thresholds specified.

With the plan finalized, the next steps are to codify Dockerfiles/worker runner and translate this spec into Terraform + CI/CD automation.
