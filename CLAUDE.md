# AI SRE Analyst — project context for Claude

## What this is

A portfolio project: an AI-powered SRE analyst for **LLM serving infrastructure on Kubernetes**. It receives Prometheus alerts about LLM workloads (TTFT degradation, cost runaways, GPU pressure, KV cache saturation), builds a per-incident timeline by merging multi-source signals (Kubernetes Events, Deployment history, Cloud Logging), retrieves relevant runbook context from Qdrant, asks Vertex AI Gemini 2.5 Flash for a structured root-cause hypothesis, and posts the verdict to Slack and a Next.js dashboard. SRE corrections feed back into the knowledge base via a curated review queue.

Built end-to-end on GCP: GKE Autopilot, Vertex AI, Firestore, GCS, Workload Identity. Deployed via Terraform + Helm.

The "AI watching your AI" framing is deliberate — LLM serving has failure modes (TTFT, cost burn, KV cache, GPU pressure) that don't show up cleanly on a generic K8s dashboard, and the project's purpose is to demonstrate I can build AI-aware observability tooling.

## Repo layout

```
services/
  ai-analyst/           FastAPI service - the main analyst pipeline
    app/
      main.py           FastAPI entrypoint, routes, lifecycle
      analyzer.py       Tier-1 deterministic rules + Tier-3 Gemini analysis
      timeline.py       Builds incident timelines from multiple signal sources
      signals/          K8s Events, Deployments, Cloud Logging collectors
      dedup.py          Redis-backed alert dedup + correlation
      vectorstore.py    Qdrant client for runbook RAG
      cost_guard.py     Severity-tiered LLM dispatch + token budget
      incident_store.py Firestore-backed durable incident records
      review_queue.py   Curated SRE feedback path (no auto-embed)
      audit.py          Append-only GCS audit log
      notifier.py       Slack + dashboard fan-out
      models.py         Pydantic models (Alert, Evidence, AnalysisResult, etc)
  mock-inference-svc/   Synthetic LLM workload emitting AI-native metrics
                        (TTFT, tokens_in/out, cost, GPU pressure, KV cache)
                        with scriptable incident modes
  kb-loader/            One-shot job that embeds runbook markdown into Qdrant
  dashboard/            Next.js 15 + Tailwind + Recharts mission-control UI
deploy/
  helm/ai-sre-analyst/  Helm chart with 7 templates
  terraform/            GKE Autopilot, Artifact Registry, GCS audit, IAM
knowledge-base/
  runbooks/             4 AI-infra runbooks (TTFT, cost, GPU, AI cascade)
docs/
  architecture.svg      The branded architecture diagram
  deploy-guide.md       Top-to-bottom deploy walkthrough
  deploy-step3-detailed.md   Stage-by-stage manual deploy reference
  linkedin-post.md      Three post variants for the portfolio writeup
  demo.md               Screen recording script
scripts/
  deploy.sh             One-shot deploy
  teardown.sh           Clean teardown
README.md               GitHub centerpiece
CLAUDE.md               This file
```

## Tech stack

| Layer | Choice |
|---|---|
| Compute | GKE Autopilot, us-central1 |
| LLM | Vertex AI gemini-2.5-flash with structured-output JSON schema |
| Embeddings | Vertex AI text-embedding-005 |
| Vector store | Qdrant (self-hosted on GKE, StatefulSet) |
| Cache | Redis (dedup TTL keys + correlation ZSET) |
| Telemetry | Prometheus + Alertmanager (kube-prometheus-stack) |
| Dashboard | Next.js 15, Tailwind, Recharts |
| Service | FastAPI 0.115, Python 3.12, async |
| Deploy | Helm 3, Terraform 1.6+ |
| Identity | Workload Identity (GKE KSA → GCP GSA) |
| State | Firestore Native (incident store + pending lessons) |
| Audit | GCS, versioned |

## The pipeline (per alert)

```
Alertmanager webhook
  → Redis dedup (5min TTL on fingerprint)
  → Redis correlate (90s sliding ZSET, cross-namespace)
  → TimelineBuilder (parallel: K8s Events + Deployments + Cloud Logs)
  → CostGuard.decide(severity)
       P3 → summary_only (no LLM, deterministic)
       P2 → llm_no_rag (Gemini, no Qdrant lookup)
       P1 → full_rag_llm (Gemini + top-k=4 runbooks)
  → Tier-1 deterministic rules (OOMKilled, CrashLoopBackOff, ImagePullBackOff,
                                 FailedScheduling, CertificateExpiry)
  → If no tier-1 hit: dispatch per cost-guard decision
  → Persist to Firestore (incidents collection)
  → Fan out to Slack + dashboard ingest + GCS audit
```

Feedback loop: SRE clicks ✏ in Slack → POST /v1/feedback → lesson goes to
`pending_lessons` in Firestore → reviewer approves via /v1/lessons/{id}/approve
→ embedded into Qdrant with provenance tags. Never auto-embed.

## Key design decisions

- **Tiered analysis** (rules → LLM) — most alerts skip the LLM entirely. Tier-1 rules are free and fast; Gemini is reserved for cases where it adds value.
- **Structured output** — Gemini is asked to return JSON conforming to a strict schema. Consumers are type-safe; system fails closed on malformed output.
- **Evidence-cited verdicts** — every AnalysisResult has an `evidence: list[Evidence]` field. Each Evidence row cites a specific timeline event with `source`, `ts`, `observation`, `weight`. No vibes.
- **Curated feedback** — corrections go to a review queue, not straight into the KB. Prevents 3am typos from poisoning Qdrant.
- **Cost guard** — daily token budget tracked in Redis, severity-tiered dispatch, soft/hard limit degradation. Better to ship deterministic verdicts than throw 503s.
- **Read-only by design** — analyst has zero write scope to the cluster. Future "AI Operator" component would be separate, gated by approval, feature-flagged.

## Mock workloads

Four heterogeneous inference workloads, each with a scripted failure mode that triggers ~3 minutes after pod start. Defined in `deploy/helm/ai-sre-analyst/values.yaml` under `mockInference.workloads`:

| Workload | Kind | Model | Incident mode |
|---|---|---|---|
| inference-chat | streaming | gemma-7b-it | ttft_spike (KV cache pressure) |
| inference-embed | embeddings | text-embedding-005 | none (healthy control) |
| inference-agents | agentic | gemini-2.5-flash | cost_runaway (output token explosion) |
| inference-canary | model rollout | gemma-7b-it-v2 | gpu_pressure (CUDA OOM ramp) |

The metrics they emit (defined in `services/mock-inference-svc/app/main.py`):
- `inference_requests_total{status, model}`
- `inference_latency_seconds`
- `inference_time_to_first_token_seconds`
- `inference_tokens_in/out_total`
- `inference_cost_usd_total` (synthesized from token counts)
- `gpu_memory_pressure_ratio`
- `kv_cache_usage_ratio`

PrometheusRules (in the Helm chart) fire alerts on these.

## Conventions

- Python: `from __future__ import annotations`, type hints everywhere, async-first, structured logging via stdlib `logging`. No mypy strict mode but types should pass mypy with no errors.
- Pydantic 2 syntax (`model_dump`, not `dict()`).
- Helm values use camelCase for top-level keys (`aiAnalyst`, `mockInference`).
- Terraform: provider 6.x, `~> 6.0` constraint. Workload Identity bindings always have an explicit `depends_on = [google_container_cluster.primary]` to avoid race conditions.
- Dashboard: prefer server components where possible; mark `"use client"` only when state/effects are needed.

## Known issues / future work

- **Dashboard renders hardcoded demo incidents.** It does not yet fetch from `/v1/incidents`. The analyst pipeline IS live; the dashboard is design-only. Wiring live fetch is a 30-min follow-up.
- **OpenTelemetry traces not yet integrated** into the timeline. Mentioned in the system prompt but not implemented.
- **No Tier-2 ML classifier** between rules and LLM — would speed up common patterns.
- **Cost-aware routing recommendations** — when agents hits cost runaway, the verdict could include a concrete `max_tokens` change to land back at baseline. Currently just describes the issue.

## Deploy / test (real GCP)

```bash
PROJECT_ID=your-project ./scripts/deploy.sh    # ~15 minutes
./scripts/teardown.sh                           # when done — DO NOT FORGET
```

Costs roughly $1.50–$3 for a 2-hour deploy + screencap. Idle GKE Autopilot still bills.

## Important deploy gotchas hit during initial setup (so we don't repeat them)

- **Apple Silicon Macs** need `--platform linux/amd64` on docker builds, otherwise pods crash with `exec format error` on x86 GKE nodes.
- **Workload Identity binding** must `depends_on` the cluster — pool is provisioned lazily, races otherwise.
- **gke-gcloud-auth-plugin** is required for kubectl ↔ GKE auth on modern kubectl. Install via `gcloud components install gke-gcloud-auth-plugin` or it ships in the Homebrew google-cloud-sdk cask.
- **kube-prometheus-stack defaults are incompatible with GKE Autopilot.** Must disable: `nodeExporter`, `kubeScheduler`, `kubeControllerManager`, `kubeProxy`, `kubeEtcd`, `coreDns`, `grafana`. The mods to `kube-system` namespace are denied by Autopilot's Warden admission webhook.
- **GCE quota** on new GCP projects is 24 vCPU/region default. The full chart fits, but with a few hundred millicores of headroom. Resource requests for Prometheus/Alertmanager have to be explicitly small.
- **Firestore Native database** must be created once per project before the analyst pod starts (it'll fail to connect otherwise). The deploy script does this idempotently.
- **kb-loader pod** must run with the analyst's ServiceAccount via `--overrides` so it inherits Workload Identity for Vertex AI embedding calls.

## How I want Claude Code to work with this repo

- **Read CLAUDE.md first.** This file. Don't re-discover the architecture — it's documented above.
- **Prefer small focused edits over large rewrites.** This codebase has a coherent shape; preserve it.
- **When changing the analyzer, vectorstore, or cost_guard, run the AST check** to verify Python parses cleanly:
  ```bash
  python3 -c "import ast; [ast.parse(open(f).read()) for f in ['services/ai-analyst/app/main.py']]"
  ```
- **Don't break the existing demo path.** The mock services + scripted incidents are how the demo reproduces. Keep them working.
- **For Helm changes**, check that `{{- if }}` and `{{- end }}` blocks balance.
- **Match existing code style.** Async-first Python, Pydantic 2, type hints, structured logging.
- **When proposing changes, explain the why before the how.** This project values rationale documented in code.
