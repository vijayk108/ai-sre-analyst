# Deploy guide: clean machine → GCP → live demo

This is a real-money walkthrough. Follow it top to bottom. Don't skip the cost section — the meter starts the second the cluster is up.

## Cost reality (April 2026 pricing)

For a 2-hour exercise: **~$1.50–$3.00**. Breakdown:

- **GKE Autopilot cluster fee**: $0.10/hour. Two hours = $0.20. (The first $74.40/month is free per billing account, so this is often $0 if you haven't used the free tier elsewhere.)
- **Pod resources**: ~$0.0445/vCPU-hour, ~$0.0049/GiB-memory-hour in us-central1. Our chart asks for ~3.5 vCPU + ~5 GiB across analyst + qdrant + redis + 4 mock services. Two hours = roughly $0.35.
- **Vertex AI Gemini 2.5 Flash**: $0.30/M input tokens, $2.50/M output. Our demo fires maybe 10–20 LLM calls total (most paths are tier-1 rules). Even with full RAG context (~1.5k input tokens, ~400 output tokens per call), that's well under $0.05 for the entire session.
- **Vertex AI text-embedding-005**: free for embeddings.
- **Firestore**: 50k reads/20k writes/day are free. We won't come close.
- **Audit GCS bucket, Artifact Registry, Cloud Logging**: pennies.
- **Networking egress**: only matters when you `docker push`. Your push goes from your machine to GCR and is free; the cluster pulling from Artifact Registry is in-region and free.

The thing that'll actually hurt you is forgetting to tear it down. Set a calendar reminder for **two hours from now** to run `./scripts/teardown.sh`.

## What you're about to install

A lot. Here's the install footprint so you know what to clean up if anything goes sideways:

| Layer | Tool | Where |
|---|---|---|
| Cloud auth | `gcloud` | local CLI |
| K8s control | `kubectl` | local CLI |
| Chart install | `helm` | local CLI |
| Cloud infra | `terraform` | local CLI |
| Image build | `docker` (Desktop or daemon) | local CLI |
| GCP project | new or existing | https://console.cloud.google.com |
| Billing account | linked to project | required for everything paid |

---

## Step 0 — install the local CLIs

If you're on macOS with Homebrew:

```bash
brew install --cask google-cloud-sdk docker
brew install kubectl helm terraform
```

On Linux (Debian/Ubuntu):

```bash
# gcloud
curl https://sdk.cloud.google.com | bash && exec -l $SHELL
# kubectl
gcloud components install kubectl
# helm
curl https://baltocdn.com/helm/signing.asc | sudo apt-key add -
sudo apt-get install apt-transport-https --yes
echo "deb https://baltocdn.com/helm/stable/debian/ all main" | sudo tee /etc/apt/sources.list.d/helm-stable-debian.list
sudo apt-get update && sudo apt-get install helm
# terraform
sudo apt-get install -y software-properties-common
wget -O- https://apt.releases.hashicorp.com/gpg | sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/hashicorp.list
sudo apt-get update && sudo apt-get install terraform
# docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER  # log out and back in after this
```

Verify:

```bash
gcloud --version
kubectl version --client
helm version
terraform --version
docker --version
```

If `docker --version` works but `docker ps` says "permission denied", you need to log out and back in for the `docker` group membership to take effect.

---

## Step 1 — set up the GCP project

```bash
# Sign in
gcloud auth login

# Create a fresh project (the project ID has to be globally unique)
PROJECT_ID="ai-sre-demo-$(date +%s)"
gcloud projects create "$PROJECT_ID" --name="AI SRE Analyst Demo"
gcloud config set project "$PROJECT_ID"

# Link a billing account — REQUIRED for everything past this point
gcloud billing accounts list
# Copy the ACCOUNT_ID from the output (looks like 01ABCD-EF1234-567890)
gcloud billing projects link "$PROJECT_ID" --billing-account=YOUR_ACCOUNT_ID

# Set up application-default credentials (this is what Terraform uses)
gcloud auth application-default login
```

Set the budget alert NOW so you don't get a surprise. Replace `your-email@example.com`:

```bash
gcloud billing budgets create \
  --billing-account=YOUR_ACCOUNT_ID \
  --display-name="ai-sre-demo cap" \
  --budget-amount=10USD \
  --threshold-rule=percent=0.5 \
  --threshold-rule=percent=0.9 \
  --threshold-rule=percent=1.0 \
  --filter-projects="projects/$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')"
```

This emails you when you hit 50%, 90%, and 100% of $10. The budget doesn't block spend — it only alerts. Tear down as soon as you're done.

---

## Step 2 — extract the repo

```bash
# Wherever you saved the tarball:
tar -xzf ai-sre-analyst.tar.gz
cd ai-sre-analyst
```

---

## Step 3 — run the deploy script

The script is idempotent (you can re-run it safely if a step fails). It does eight things:

1. Terraform `apply`: enables 9 GCP APIs, creates GKE Autopilot cluster, Artifact Registry repo, audit GCS bucket, IAM service accounts.
2. Configures `kubectl` to point at the new cluster.
3. Configures Docker to push to Artifact Registry.
4. Builds and pushes 3 container images (analyst, mock-inference-svc, kb-loader).
5. Creates the Firestore Native database (one-time per project).
6. Installs `kube-prometheus-stack` (Prometheus + Alertmanager + Grafana operator).
7. Installs the `ai-sre-analyst` Helm chart.
8. Runs the kb-loader job to embed runbook chunks into Qdrant.

Total time: **~12–15 minutes** on a clean GCP project. Most of that is GKE Autopilot cluster provisioning (~6–8 min) and Docker image builds (~3 min).

```bash
chmod +x scripts/deploy.sh scripts/teardown.sh
PROJECT_ID="$PROJECT_ID" ./scripts/deploy.sh
```

You'll see eight `>>` markers as it progresses. If anything fails, scroll up to find the first error — usually it's "API not enabled yet, propagation in progress, retry in 60 seconds." If that's the message, just re-run the script.

If Terraform fails on `google_storage_bucket.audit` complaining about retention lock, edit `deploy/terraform/main.tf` to set `is_locked = false` and `force_destroy = true`, then re-run.

---

## Step 4 — verify the install

```bash
# All ai-sre pods should be Running
kubectl get pods -n ai-sre
# Should show: ai-analyst-*, qdrant-0, redis-*

# Mock workloads in their own namespaces
for ns in inference-chat inference-embed inference-agents inference-canary; do
  echo "=== $ns ==="
  kubectl get pods -n $ns
done

# Prometheus picked them up?
kubectl -n monitoring port-forward svc/prometheus-operated 9090:9090 &
# In a browser: http://localhost:9090/targets
# You should see ~10 healthy targets across the four inference- namespaces
```

If pods are stuck in `Pending`, GKE Autopilot is still scaling up nodes — give it 3 minutes. If they're in `ImagePullBackOff`, the registry auth didn't take; re-run step 3 of `deploy.sh`.

---

## Step 5 — connect to the dashboard

The dashboard isn't exposed publicly (no Ingress in this chart). Port-forward it:

```bash
# In a new terminal
kubectl -n ai-sre port-forward svc/dashboard 3000:3000
```

Open `http://localhost:3000`.

> **Heads-up about the dashboard**: in this build the dashboard renders three hardcoded incidents and the four hardcoded namespace cards as a design reference. The analyst's `/v1/incidents` endpoint is live and queryable directly, but the dashboard isn't yet wired to fetch from it (this was deliberate — the prior priority was the design and the analyst pipeline; live-fetch is a 30-min follow-up if you want it). For now: the dashboard shows what a verdict *looks like*; the actual live verdicts are visible via Slack and the analyst logs. To pull live data manually:
>
> ```bash
> ANALYST=$(kubectl -n ai-sre get svc ai-analyst -o jsonpath='{.spec.clusterIP}')
> kubectl -n ai-sre run curl-incidents --rm -i --restart=Never --image=curlimages/curl -- \
>   curl -s "http://${ANALYST}:8080/v1/incidents" | jq
> ```

You should see the four namespace cards. For the **first 3 minutes** after install, all four show as healthy because the scripted incidents haven't fired yet. After 3 minutes:

- `inference-chat` goes amber (TTFT spike)
- `inference-embed` stays green (control)
- `inference-agents` goes red (cost runaway)
- `inference-canary` goes amber then climbs (GPU pressure)

Wait until the dashboard shows two or three live verdicts in the incident list. Each verdict cites real K8s Events the analyst pulled from the cluster.

---

## Step 6 — connect Slack (optional but recommended)

Slack is what really sells the demo in a screen recording.

1. In your throwaway Slack workspace, create a channel like `#ai-sre-demo`
2. Add an "Incoming Webhook" app to that channel: https://api.slack.com/messaging/webhooks
3. Copy the webhook URL (starts with `https://hooks.slack.com/services/...`)
4. Inject it as a Kubernetes secret:

```bash
kubectl -n ai-sre create secret generic slack-webhook \
  --from-literal=url="https://hooks.slack.com/services/YOUR/WEBHOOK/URL"

# Bounce the analyst pods so they pick it up
kubectl -n ai-sre rollout restart deployment/ai-analyst
```

The next verdict will arrive in Slack with the `✓ Correct` / `✏ Correct it` buttons.

---

## Step 7 — exercise the LLM path

By default, only ~half the alerts hit the LLM (tier-1 rules catch the rest). To force a tier-3 verdict for the screen recording:

```bash
# Manually post a synthetic alert that won't match any tier-1 rule
ANALYST=$(kubectl -n ai-sre get svc ai-analyst -o jsonpath='{.spec.clusterIP}')
kubectl -n ai-sre run curl-test --rm -i --restart=Never --image=curlimages/curl -- \
  curl -sX POST "http://${ANALYST}:8080/v1/alerts" \
  -H "Content-Type: application/json" \
  -d '{
    "version": "4",
    "receiver": "ai-analyst",
    "status": "firing",
    "alerts": [{
      "status": "firing",
      "labels": {
        "alertname": "InferenceCostSpike",
        "namespace": "inference-agents",
        "service": "inference",
        "severity": "critical"
      },
      "annotations": {
        "summary": "Output token rate 8.7x baseline on inference-agents",
        "description": "Cost burn rate at $6.84/min, RPS unchanged"
      },
      "startsAt": "'$(date -u +%Y-%m-%dT%H:%M:%SZ)'",
      "fingerprint": "test-cost-spike-001"
    }]
  }'
```

Watch the analyst pod logs in another terminal:

```bash
kubectl -n ai-sre logs -l app.kubernetes.io/name=ai-analyst -f
```

You'll see the timeline build, the cost-guard decision (should be `full_rag_llm` for severity=critical), the Vertex AI call, and the verdict get fanned out to Slack and Firestore.

---

## Step 8 — tear it down

**Don't skip this.** Even an idle GKE Autopilot cluster bills.

```bash
PROJECT_ID="$PROJECT_ID" ./scripts/teardown.sh
```

Verify in the GCP console:
- https://console.cloud.google.com/kubernetes/list — no clusters
- https://console.cloud.google.com/artifacts — no repositories
- https://console.cloud.google.com/storage/browser — no `*-ai-sre-audit` bucket

If you're done with the project entirely:

```bash
gcloud projects delete "$PROJECT_ID"
```

This also cleans up Firestore, which terraform doesn't touch.

---

## Troubleshooting

**"API has not been used in project"** during terraform apply: the API was just enabled and hasn't propagated yet. Wait 60s, re-run.

**Pods in `ImagePullBackOff`**: registry auth didn't apply. Run `gcloud auth configure-docker us-central1-docker.pkg.dev --quiet` and `kubectl -n ai-sre rollout restart deployment/ai-analyst`.

**`gcloud firestore databases create` fails with "ALREADY_EXISTS"**: that's fine, the script ignores this error.

**`terraform destroy` fails on the audit bucket**: the 30-day retention is locked. You'll need to wait it out, or — if you're certain you don't need the audit data — go into the GCS console and force-delete the bucket and its objects manually.

**No verdicts appearing on the dashboard**: check that Alertmanager is configured. The `AlertmanagerConfig` CRD only takes effect if the kube-prometheus-stack release is installed (step 6 of deploy.sh). Verify with `kubectl -n monitoring get alertmanagerconfigs.monitoring.coreos.com -A`.

**Verdicts say "Model returned malformed output"**: Vertex AI quota issue or the model returned non-JSON. Check `kubectl -n ai-sre logs -l app.kubernetes.io/name=ai-analyst | grep Gemini`. The cost guard's fallback verdict is what you'll see on the dashboard while you debug.
