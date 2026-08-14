#!/usr/bin/env bash
# Teardown the AI SRE Analyst stack. Run this when you're done filming.
#
# What this destroys:
#   - The Helm release (analyst + dependencies + mock workloads)
#   - The kube-prometheus-stack release
#   - The GKE cluster, Artifact Registry, audit bucket, IAM bindings
#
# What this does NOT destroy:
#   - Firestore database (it's per-project, not per-environment).
#     Empty it manually if needed.
#   - Cloud Logging entries (subject to log retention policy).
#
# IMPORTANT: the audit bucket has a 30-day retention policy locked.
# `terraform destroy` will fail if you try to delete it inside that
# window. If you're tearing down for a re-deploy, edit
# deploy/terraform/main.tf to set:
#   - retention_policy.is_locked = false
#   - force_destroy = true
# before the FIRST apply.

set -euo pipefail

: "${PROJECT_ID:?PROJECT_ID env var is required}"
REGION="${REGION:-us-central1}"
CLUSTER="${CLUSTER:-ai-sre-analyst}"
NAMESPACE="${NAMESPACE:-ai-sre}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo ">> 1. Uninstalling Helm releases (best-effort)"
helm uninstall ai-sre-analyst -n "$NAMESPACE" 2>/dev/null || echo "   (not installed)"
helm uninstall prometheus -n monitoring 2>/dev/null || echo "   (not installed)"

echo ">> 2. Deleting application namespaces"
for ns in "$NAMESPACE" inference-chat inference-embed inference-agents inference-canary monitoring; do
  kubectl delete namespace "$ns" --ignore-not-found --wait=false
done

echo ">> 3. Destroying GCP infrastructure with Terraform"
( cd deploy/terraform && terraform destroy -auto-approve \
    -var "project_id=${PROJECT_ID}" -var "region=${REGION}" -var "cluster_name=${CLUSTER}" )

echo ">> Done. Verify in the GCP console that no GKE cluster, Artifact Registry,"
echo "   or audit bucket remain. Firestore is NOT torn down — clear it manually"
echo "   if you want to: gcloud firestore databases delete --database='(default)'"
