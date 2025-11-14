locals {
  region_short = replace(var.region, "-", "")

  bucket_uploads = "va-rag-uploads-prod"
  bucket_temp    = "va-rag-worker-temp-prod"

  pubsub_topic_ingestion = "ingestion-documents"
  pubsub_topic_dlq       = "ingestion-documents-dlq"
  pubsub_subscription    = "ingestion-worker-sub"

  service_accounts = {
    api    = "svc-rag-api"
    worker = "svc-rag-worker"
    ci     = "svc-ci"
  }

  log_dataset_id = "va_prod_traces"
}
