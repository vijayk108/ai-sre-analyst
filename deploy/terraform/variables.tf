variable "project_id" {
  description = "GCP project to deploy into"
  type        = string
}

variable "region" {
  description = "Region for GKE Autopilot and Artifact Registry"
  type        = string
  default     = "us-central1"
}

variable "cluster_name" {
  description = "Name of the GKE Autopilot cluster"
  type        = string
  default     = "ai-sre-analyst"
}
