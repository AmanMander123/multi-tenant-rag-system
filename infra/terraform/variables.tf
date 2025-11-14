variable "project_id" {
  description = "Google Cloud project ID."
  type        = string
  default     = "virtual-assistant-460209"
}

variable "region" {
  description = "Primary GCP region for regional resources."
  type        = string
  default     = "us-central1"
}

variable "location" {
  description = "Multi-region location for Artifact Registry / BigQuery."
  type        = string
  default     = "us"
}

variable "api_image" {
  description = "Container image for the ingestion API Cloud Run service."
  type        = string
}

variable "worker_image" {
  description = "Container image for the worker Cloud Run service."
  type        = string
}

variable "github_repository" {
  description = "GitHub repository in OWNER/REPO format allowed to deploy via Workload Identity Federation."
  type        = string
  default     = "amanmander123/multi-tenant-rag-system"
}

variable "supabase_url" {
  description = "Base Supabase URL used by the API."
  type        = string
  default     = "https://virtualassistant460209.supabase.co"
}

variable "supabase_jwks_url" {
  description = "JWKS endpoint for Supabase-issued JWTs."
  type        = string
  default     = "https://virtualassistant460209.supabase.co/auth/v1/jwks"
}

variable "supabase_jwt_audience" {
  description = "Supabase JWT audience claim enforced by the API."
  type        = string
  default     = "auth.virtualassistant460209.supabase.co"
}

variable "firestore_collection_namespace" {
  description = "Firestore collection used for tenant configs."
  type        = string
  default     = "tenants"
}
