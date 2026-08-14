#!/usr/bin/env bash
# Deploy the AI SRE Analyst stack to GKE.
#
# Prerequisites:
#   - gcloud authenticated to a project where you have Owner/Editor
#   - terraform >= 1.6, helm >= 3.12, kubectl, docker
#   - $PROJECT_ID env var set
#
# Usage:
#   PROJECT_ID=my-gcp-project ./scripts/deploy.sh

set -euo pipefail

: "${PROJECT_ID:?PROJECT_ID env var is required}"
REGION="${REGION:-us-central1}"
CLUSTER="${CLUSTER:-ai-sre-analyst}"
NAMESPACE="${NAMESPACE:-ai-sre}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo ">> 1. Provisioning GCP infrastructure with Terraform"
( cd deploy/terraform && terraform init -upgrade && terraform apply -auto-approve \
    -var "project_id=${PROJECT_ID}" -var "region=${REGION}" -var "cluster_name=${CLUSTER}" )

REGISTRY="$(cd deploy/terraform && terraform output -raw registry)"
AUDIT_BUCKET="$(cd deploy/terraform && terraform output -raw audit_bucket)"

echo ">> 2. Configuring kubectl for the new cluster"
gcloud container clusters get-credentials "$CLUSTER" --region "$REGION" --project "$PROJECT_ID"

echo ">> 3. Configuring Docker auth for Artifact Registry"
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

echo ">> 4. Building and pushing service images"
for svc in ai-analyst mock-inference-svc kb-loader dashboard; do
  echo "   - $svc"
  docker build -f "services/${svc}/Dockerfile" -t "${REGISTRY}/${svc}:1.0.0" .
  docker push "${REGISTRY}/${svc}:1.0.0"
done

echo ">> 5. Initializing Firestore (Native mode) — first-run only"
# Firestore requires a database to exist. Native mode, regional location.
# This is idempotent: a no-op if the database already exists.
gcloud firestore databases create \
  --location="${REGION}" \
  --type=firestore-native \
  --project="${PROJECT_ID}" 2>/dev/null || \
  echo "   (database already exists, skipping)"

echo ">> 6. Installing Prometheus operator (if missing)"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update >/dev/null
helm upgrade --install prometheus prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace --wait

echo ">> 7. Installing the AI SRE Analyst chart"
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install ai-sre-analyst deploy/helm/ai-sre-analyst \
  --namespace "$NAMESPACE" \
  --set "global.gcpProject=${PROJECT_ID}" \
  --set "global.gcpLocation=${REGION}" \
  --set "global.imageRegistry=${REGISTRY}" \
  --set "aiAnalyst.env.auditBucket=${AUDIT_BUCKET}" \
  --set "aiAnalyst.serviceAccount.annotations.iam\.gke\.io/gcp-service-account=ai-analyst@${PROJECT_ID}.iam.gserviceaccount.com" \
  --wait

echo ">> 8. Seeding the runbook knowledge base"
kubectl -n "$NAMESPACE" run kb-loader --rm -i --restart=Never \
  --image="${REGISTRY}/kb-loader:1.0.0" \
  --env="QDRANT_URL=http://qdrant.${NAMESPACE}.svc.cluster.local:6333" \
  --env="GCP_PROJECT=${PROJECT_ID}" \
  --env="GCP_LOCATION=${REGION}"

echo ">> Done. Tail the analyst logs with:"
echo "   kubectl -n ${NAMESPACE} logs -l app.kubernetes.io/name=ai-analyst -f"
