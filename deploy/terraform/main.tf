terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# --- API enablement -------------------------------------------------------
resource "google_project_service" "apis" {
  for_each = toset([
    "container.googleapis.com",
    "artifactregistry.googleapis.com",
    "aiplatform.googleapis.com",
    "firestore.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "storage.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

# --- Artifact Registry for the service images ----------------------------
resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = "ai-sre-analyst"
  format        = "DOCKER"
  description   = "Container images for the AI SRE Analyst platform"
  depends_on    = [google_project_service.apis]
}

# --- GKE Autopilot cluster -----------------------------------------------
resource "google_container_cluster" "primary" {
  name             = var.cluster_name
  location         = var.region
  enable_autopilot = true
  deletion_protection = false

  release_channel {
    channel = "REGULAR"
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  depends_on = [google_project_service.apis]
}

# --- GCS bucket for the immutable audit log ------------------------------
resource "google_storage_bucket" "audit" {
  name                        = "${var.project_id}-ai-sre-audit"
  location                    = var.region
  # force_destroy=true so `terraform destroy` succeeds even with objects
  # in the bucket. For a real production deploy, set this back to false
  # and accept the manual cleanup step. Audit data here is synthetic.
  force_destroy               = true
  uniform_bucket_level_access = true

  versioning { enabled = true }

  # NOTE: a real production deploy would have a retention_policy here
  # (30+ days) for compliance. We omit it for the demo because it
  # conflicts with force_destroy=true. To re-enable for production:
  #
  #   retention_policy {
  #     retention_period = 60 * 60 * 24 * 30  # 30 days
  #     is_locked        = false
  #   }
  #   force_destroy = false  # then deletion needs manual cleanup

  lifecycle_rule {
    condition {
      age = 365
    }
    action {
      type          = "SetStorageClass"
      storage_class = "COLDLINE"
    }
  }
}

# --- GSA used by the AI Analyst pod via Workload Identity ----------------
resource "google_service_account" "ai_analyst" {
  account_id   = "ai-analyst"
  display_name = "AI SRE Analyst workload"
}

resource "google_project_iam_member" "vertex_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.ai_analyst.email}"
}

resource "google_project_iam_member" "logging_viewer" {
  project = var.project_id
  role    = "roles/logging.viewer"
  member  = "serviceAccount:${google_service_account.ai_analyst.email}"
}

resource "google_storage_bucket_iam_member" "audit_writer" {
  bucket = google_storage_bucket.audit.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.ai_analyst.email}"
}

# Firestore (Native mode) is enabled per-project; we reference it by
# binding datastore.user, which covers Firestore Native API access.
resource "google_project_iam_member" "firestore_user" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.ai_analyst.email}"
}

# Bind GKE KSA -> GSA for Workload Identity.
# IMPORTANT: depends_on the cluster — the Workload Identity Pool
# (PROJECT_ID.svc.id.goog) is provisioned lazily by GKE after cluster
# creation completes, and Terraform's implicit dependency graph
# doesn't catch this. Without depends_on you'll race the pool's
# creation and get "Identity Pool does not exist" intermittently.
resource "google_service_account_iam_member" "wi_binding" {
  service_account_id = google_service_account.ai_analyst.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[ai-sre/ai-analyst-sa]"

  depends_on = [google_container_cluster.primary]
}

# --- Outputs --------------------------------------------------------------
output "cluster_name" {
  value = google_container_cluster.primary.name
}

output "registry" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "audit_bucket" {
  value = google_storage_bucket.audit.name
}

output "ai_analyst_sa_email" {
  value = google_service_account.ai_analyst.email
}
