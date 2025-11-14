terraform {
  backend "gcs" {
    bucket = "virtual-assistant-460209-tf-state"
    prefix = "env/prod"
  }
}
