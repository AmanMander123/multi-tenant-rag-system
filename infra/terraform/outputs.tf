output "api_service_uri" {
  description = "Deployed Cloud Run URL for the ingestion API."
  value       = google_cloud_run_v2_service.api.uri
}

output "worker_service_name" {
  description = "Identifier for the worker Cloud Run service."
  value       = google_cloud_run_v2_service.worker.name
}

output "github_workload_identity_provider" {
  description = "Fully-qualified provider resource for GitHub OIDC."
  value       = google_iam_workload_identity_pool_provider.github.name
}
