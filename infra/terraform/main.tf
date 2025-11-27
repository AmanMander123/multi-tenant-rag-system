# Service Accounts
resource "google_service_account" "api" {
  account_id   = local.service_accounts.api
  display_name = "RAG Ingestion API"
}

resource "google_service_account" "worker" {
  account_id   = local.service_accounts.worker
  display_name = "RAG Worker"
}

resource "google_service_account" "ci" {
  account_id   = local.service_accounts.ci
  display_name = "CI/CD Deploy Identity"
}

# Storage Buckets
resource "google_storage_bucket" "uploads" {
  name                        = local.bucket_uploads
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false

  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      age = 30
    }
  }
}

resource "google_storage_bucket" "worker_temp" {
  name                        = local.bucket_temp
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false

  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      age = 7
    }
  }
}

# Pub/Sub topics and subscription
resource "google_pubsub_topic" "ingestion" {
  name   = local.pubsub_topic_ingestion
  labels = { service = "rag-ingestion" }
}

resource "google_pubsub_topic" "ingestion_dlq" {
  name   = local.pubsub_topic_dlq
  labels = { service = "rag-ingestion" }
}

resource "google_pubsub_subscription" "worker" {
  name  = local.pubsub_subscription
  topic = google_pubsub_topic.ingestion.id

  ack_deadline_seconds    = 60
  retain_acked_messages   = false
  enable_message_ordering = false
  expiration_policy {
    ttl = ""
  }
  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.ingestion_dlq.id
    max_delivery_attempts = 5
  }
  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }
  labels = { service = "rag-worker" }
}

# Secret data lookups
data "google_secret_manager_secret" "openai" {
  project   = var.project_id
  secret_id = "openai-api-key"
}

data "google_secret_manager_secret" "pinecone" {
  project   = var.project_id
  secret_id = "pinecone-api-key"
}

data "google_secret_manager_secret" "supabase_jwt_aud" {
  project   = var.project_id
  secret_id = "supabase-jwt-aud"
}

data "google_secret_manager_secret" "supabase_db_password" {
  project   = var.project_id
  secret_id = "supabase-db-password"
}

data "google_secret_manager_secret" "supabase_db_url" {
  project   = var.project_id
  secret_id = "supabase-db-url"
}

# Secret IAM bindings
resource "google_secret_manager_secret_iam_member" "api_supabase_config" {
  project   = var.project_id
  secret_id = data.google_secret_manager_secret.supabase_jwt_aud.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.api.email}"
}

resource "google_secret_manager_secret_iam_member" "worker_openai" {
  project   = var.project_id
  secret_id = data.google_secret_manager_secret.openai.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_secret_manager_secret_iam_member" "worker_pinecone" {
  project   = var.project_id
  secret_id = data.google_secret_manager_secret.pinecone.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_secret_manager_secret_iam_member" "worker_supabase_db" {
  project   = var.project_id
  secret_id = data.google_secret_manager_secret.supabase_db_password.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_secret_manager_secret_iam_member" "worker_supabase_db_url" {
  project   = var.project_id
  secret_id = data.google_secret_manager_secret.supabase_db_url.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.worker.email}"
}

# Bucket IAM
resource "google_storage_bucket_iam_member" "api_uploader" {
  bucket = google_storage_bucket.uploads.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.api.email}"
}

resource "google_storage_bucket_iam_member" "worker_reader" {
  bucket = google_storage_bucket.uploads.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_storage_bucket_iam_member" "worker_temp_writer" {
  bucket = google_storage_bucket.worker_temp.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.worker.email}"
}

# Project-level IAM
resource "google_project_iam_member" "api_pubsub_publisher" {
  project = var.project_id
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_service_account.api.email}"
}

resource "google_project_iam_member" "worker_pubsub_subscriber" {
  project = var.project_id
  role    = "roles/pubsub.subscriber"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_project_iam_member" "ci_run_admin" {
  project = var.project_id
  role    = "roles/run.admin"
  member  = "serviceAccount:${google_service_account.ci.email}"
}

resource "google_project_iam_member" "ci_iam_user" {
  project = var.project_id
  role    = "roles/iam.serviceAccountUser"
  member  = "serviceAccount:${google_service_account.ci.email}"
}

resource "google_project_iam_member" "ci_storage_admin" {
  project = var.project_id
  role    = "roles/storage.admin"
  member  = "serviceAccount:${google_service_account.ci.email}"
}

resource "google_project_iam_member" "ci_pubsub_admin" {
  project = var.project_id
  role    = "roles/pubsub.admin"
  member  = "serviceAccount:${google_service_account.ci.email}"
}

# Workload Identity Federation
resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github-pool"
  display_name              = "GitHub Actions Pool"
  description               = "OIDC pool for GitHub Actions deployments."
  project                   = var.project_id
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-provider"
  display_name                       = "GitHub Provider"
  description                        = "OIDC provider for GitHub Actions."
  project                            = var.project_id

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
  }

  attribute_condition = "assertion.repository == \"${var.github_repository}\""
}

resource "google_service_account_iam_member" "ci_wif" {
  service_account_id = google_service_account.ci.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repository}"
}

# Cloud Armor policy (allow all for now)
resource "google_compute_security_policy" "default" {
  name        = "va-prod-default"
  description = "Placeholder policy - currently allow all traffic."

  rule {
    priority = 1000
    action   = "allow"
    match {
      versioned_expr = "SRC_IPS_V1"
      config { src_ip_ranges = ["*"] }
    }
  }

  rule {
    priority = 2147483647
    action   = "deny(403)"
    match {
      versioned_expr = "SRC_IPS_V1"
      config { src_ip_ranges = ["*"] }
    }
  }
}


# BigQuery dataset for log sink
resource "google_bigquery_dataset" "logs" {
  dataset_id  = local.log_dataset_id
  project     = var.project_id
  location    = var.location
  description = "Structured Cloud Run logs for observability dashboards."
}

resource "google_logging_project_sink" "logs_to_bigquery" {
  name        = "va-prod-traces"
  description = "Route Cloud Run logs to BigQuery for analytics."
  destination = "bigquery.googleapis.com/projects/${var.project_id}/datasets/${google_bigquery_dataset.logs.dataset_id}"
  filter      = "resource.type=\"cloud_run_revision\""

  unique_writer_identity = true
}

resource "google_bigquery_dataset_iam_member" "logs_writer" {
  dataset_id = google_bigquery_dataset.logs.dataset_id
  project    = var.project_id
  role       = "roles/bigquery.dataEditor"
  member     = google_logging_project_sink.logs_to_bigquery.writer_identity
}

# Cloud Run services
resource "google_cloud_run_v2_service" "api" {
  name     = "rag-api"
  location = var.region
  depends_on = [
    google_storage_bucket_iam_member.api_uploader,
    google_secret_manager_secret_iam_member.api_supabase_config,
    google_pubsub_topic.ingestion
  ]

  template {
    service_account = google_service_account.api.email

    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }

    containers {
      image = var.api_image
      ports {
        container_port = 8080
      }

      env {
        name  = "APP_ENV"
        value = "prod"
      }

      env {
        name  = "GCLOUD_PROJECT"
        value = var.project_id
      }

      env {
        name  = "PUBSUB_TOPIC_INGEST"
        value = google_pubsub_topic.ingestion.id
      }

      env {
        name  = "GCS_UPLOAD_BUCKET"
        value = google_storage_bucket.uploads.name
      }

      env {
        name = "SUPABASE_DB_URL"
        value_source {
          secret_key_ref {
            secret  = data.google_secret_manager_secret.supabase_db_url.id
            version = "latest"
          }
        }
      }

      env {
        name = "SUPABASE_DB_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = data.google_secret_manager_secret.supabase_db_password.id
            version = "latest"
          }
        }
      }

      env {
        name = "OPENAI_API_KEY"
        value_source {
          secret_key_ref {
            secret  = data.google_secret_manager_secret.openai.id
            version = "latest"
          }
        }
      }

      env {
        name = "PINECONE_API_KEY"
        value_source {
          secret_key_ref {
            secret  = data.google_secret_manager_secret.pinecone.id
            version = "latest"
          }
        }
      }

      env {
        name  = "SUPABASE_URL"
        value = var.supabase_url
      }

      env {
        name  = "SUPABASE_JWKS_URL"
        value = var.supabase_jwks_url
      }

      env {
        name  = "FIRESTORE_COLLECTION_NAMESPACE"
        value = var.firestore_collection_namespace
      }

      env {
        name = "SUPABASE_JWT_AUDIENCE"
        value_source {
          secret_key_ref {
            secret  = data.google_secret_manager_secret.supabase_jwt_aud.id
            version = "latest"
          }
        }
      }

    }
  }

  ingress      = "INGRESS_TRAFFIC_ALL"
  launch_stage = "BETA"
}

resource "google_cloud_run_v2_service" "worker" {
  name     = "rag-worker"
  location = var.region
  depends_on = [
    google_storage_bucket_iam_member.worker_reader,
    google_storage_bucket_iam_member.worker_temp_writer,
    google_secret_manager_secret_iam_member.worker_openai,
    google_secret_manager_secret_iam_member.worker_pinecone,
    google_secret_manager_secret_iam_member.worker_supabase_db,
    google_secret_manager_secret_iam_member.worker_supabase_db_url,
    google_pubsub_subscription.worker
  ]

  template {
    service_account = google_service_account.worker.email

    scaling {
      min_instance_count = 0
      max_instance_count = 4
    }

    containers {
      image = var.worker_image

      args = [
        "run",
        "uvicorn",
        "app.workers.push_app:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8080",
      ]
      command = ["uv"]

      env {
        name  = "APP_ENV"
        value = "prod"
      }

      env {
        name  = "GCLOUD_PROJECT"
        value = var.project_id
      }

      env {
        name  = "GCS_UPLOAD_BUCKET"
        value = google_storage_bucket.uploads.name
      }

      env {
        name  = "GCS_TEMP_BUCKET"
        value = google_storage_bucket.worker_temp.name
      }

      env {
        name  = "FIRESTORE_COLLECTION_NAMESPACE"
        value = var.firestore_collection_namespace
      }

      env {
        name = "SUPABASE_DB_URL"
        value_source {
          secret_key_ref {
            secret  = data.google_secret_manager_secret.supabase_db_url.id
            version = "latest"
          }
        }
      }

      env {
        name = "OPENAI_API_KEY"
        value_source {
          secret_key_ref {
            secret  = data.google_secret_manager_secret.openai.id
            version = "latest"
          }
        }
      }

      env {
        name = "PINECONE_API_KEY"
        value_source {
          secret_key_ref {
            secret  = data.google_secret_manager_secret.pinecone.id
            version = "latest"
          }
        }
      }

      env {
        name = "SUPABASE_DB_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = data.google_secret_manager_secret.supabase_db_password.id
            version = "latest"
          }
        }
      }
    }
  }

  ingress      = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  launch_stage = "BETA"
}

resource "google_cloud_run_v2_job" "reindex" {
  name     = "rag-reindex"
  location = var.region
  depends_on = [
    google_storage_bucket_iam_member.worker_reader,
    google_storage_bucket_iam_member.worker_temp_writer,
    google_secret_manager_secret_iam_member.worker_openai,
    google_secret_manager_secret_iam_member.worker_pinecone,
    google_secret_manager_secret_iam_member.worker_supabase_db,
    google_secret_manager_secret_iam_member.worker_supabase_db_url
  ]

  template {
    template {
      service_account = google_service_account.worker.email
      containers {
        image = var.worker_image
        command = ["uv"]
        args = ["run", "python", "-m", "app.workers.reindex_job"]

        env {
          name  = "APP_ENV"
          value = "prod"
        }

        env {
          name  = "GCLOUD_PROJECT"
          value = var.project_id
        }

        env {
          name  = "GCS_UPLOAD_BUCKET"
          value = google_storage_bucket.uploads.name
        }

        env {
          name  = "GCS_TEMP_BUCKET"
          value = google_storage_bucket.worker_temp.name
        }

        env {
          name = "SUPABASE_DB_URL"
          value_source {
            secret_key_ref {
              secret  = data.google_secret_manager_secret.supabase_db_url.id
              version = "latest"
            }
          }
        }

        env {
          name = "OPENAI_API_KEY"
          value_source {
            secret_key_ref {
              secret  = data.google_secret_manager_secret.openai.id
              version = "latest"
            }
          }
        }

        env {
          name = "PINECONE_API_KEY"
          value_source {
            secret_key_ref {
              secret  = data.google_secret_manager_secret.pinecone.id
              version = "latest"
            }
          }
        }

        env {
          name = "SUPABASE_DB_PASSWORD"
          value_source {
            secret_key_ref {
              secret  = data.google_secret_manager_secret.supabase_db_password.id
              version = "latest"
            }
          }
        }
      }
    }
  }
}

resource "google_cloud_run_service_iam_member" "api_public" {
  location = var.region
  project  = var.project_id
  service  = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# Monitoring alerts
resource "google_monitoring_alert_policy" "pubsub_backlog" {
  display_name = "Ingestion backlog latency"
  combiner     = "OR"

  conditions {
    display_name = "Oldest unacked message > 120s"
    condition_threshold {
      filter          = "metric.type=\"pubsub.googleapis.com/subscription/oldest_unacked_message_age\" resource.type=\"pubsub_subscription\" resource.label.\"subscription_id\"=\"${google_pubsub_subscription.worker.name}\""
      duration        = "0s"
      comparison      = "COMPARISON_GT"
      threshold_value = 120
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }
}

resource "google_monitoring_alert_policy" "pubsub_dlq" {
  display_name = "DLQ message spike"
  combiner     = "OR"

  conditions {
    display_name = "DLQ count >= 5"
    condition_threshold {
      filter          = "metric.type=\"pubsub.googleapis.com/subscription/dead_letter_message_count\" resource.type=\"pubsub_subscription\" resource.label.\"subscription_id\"=\"${google_pubsub_subscription.worker.name}\""
      duration        = "0s"
      comparison      = "COMPARISON_GT"
      threshold_value = 5
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }
}

resource "google_cloud_scheduler_job" "reindex_nightly" {
  name        = "rag-reindex-nightly"
  description = "Nightly reindex/backfill trigger"
  schedule    = "0 9 * * *"
  time_zone   = "Etc/UTC"

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/apis/run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.reindex.name}:run"

    oidc_token {
      service_account_email = google_service_account.ci.email
    }

    headers = {
      "Content-Type" = "application/json"
    }

    body = base64encode("{\"overrides\":{}}")
  }
}
