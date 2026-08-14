# Demo script — what to show on a screen recording

A 3-4 minute screencap covers everything important. Run this with two terminals and a browser open.

## Setup (off-camera, ~10 min before recording)

```bash
PROJECT_ID=your-project ./scripts/deploy.sh
kubectl -n ai-sre logs -l app.kubernetes.io/name=ai-analyst -f > /tmp/analyst.log &
```

Open these tabs:
1. **Dashboard** — `http://localhost:3000` (port-forward the dashboard service)
2. **Slack** — channel where the webhook posts
3. **Architecture diagram** — `docs/architecture.svg` (for the intro shot)

Wait until the mock services have run for ~3 minutes so the scripted incidents have started firing. The scenarios you should be able to see:

- `inference-chat` — TTFT climbing, KV cache pinned
- `inference-agents` — cost rate jumping while RPS is flat (the "agent loop" scenario)
- `inference-canary` — GPU memory pressure ramping
- `inference-embed` — healthy contrast

## Recording (target: 3-4 minutes)

### 0:00 — The hook (15s)
Open the dashboard hero. Talk through the framing in one breath:
> "This is an AI SRE analyst that watches LLM serving infrastructure. LLM serving has failure modes that don't show up on a generic K8s dashboard — TTFT degradation, output token explosions, GPU memory pressure. The analyst is here to catch them."

### 0:15 — Architecture (30s)
Cut to `architecture.svg` full-screen.
> "Prometheus scrapes four inference workloads — chat, embeddings, agents, and a canary release. Alertmanager forwards to a FastAPI service, which dedupes through Redis, retrieves runbook context from Qdrant, asks Gemini for a verdict, and fans out to Slack, a dashboard, and an immutable audit log."

### 0:45 — Workload grid (30s)
Back to the dashboard. Hover over each namespace card:
- "Chat is showing TTFT degradation — that amber spike is KV cache saturation"
- "Embeddings is fine — it's the control"
- "Agents is the critical one — note the cost rate, $6.84 per minute, this is an agent loop"
- "Canary is showing the GPU memory ratio climbing toward OOM"

### 1:15 — The cost runaway verdict (75s)
Click the agents incident card to expand. Read out:
- The root cause: "Output tokens are 8.7× baseline while RPS is unchanged. The agents-runtime release at 18:39 dropped a recursion guard."
- The blast radius: "agents/inference, billing rollup, Vertex AI quota burn"
- The remediation steps — emphasize step 1: "Lower max_tokens to cap the bleeding immediately. Cost rate is real money."
- The runbooks consulted

This is the moment that sells the post. Pause on it.

### 2:30 — Slack handoff (30s)
Cut to Slack. Show the same verdict arriving with the `✓ Correct` / `✏ Correct it` buttons. Click `✏ Correct it` and type a one-line correction — for example: "Also check whether the recursion guard was removed in the orchestration layer, not just the prompt template." Mention:
> "That correction gets embedded and upserted back into Qdrant. Next time a similar incident fires, this guidance shows up as context for the model."

### 3:00 — The pipeline in logs (30s)
Cut to the terminal tailing analyst logs:
```
2026-04-28 18:43:01 INFO ai-analyst :: Suppressed duplicate alert ...
2026-04-28 18:43:14 INFO ai-analyst.dedup :: ...
2026-04-28 18:43:15 INFO ai-analyst :: Analysis ... — 3_llm confidence=0.93
```
Point out: "Dedup, RAG, Gemini call, fan-out — about 1.4 seconds end-to-end."

### 3:30 — Tier-1 path (15s)
Find the `GPUMemoryPressure` verdict in the dashboard tagged `tier-1`. Mention:
> "This one didn't need an LLM call. Tier-1 handles known shapes deterministically — sub-second, no token spend. The LLM is reserved for the cases where it actually adds value."

### 3:45 — Wrap (15s)
Back to the dashboard hero shot or the repo URL.
> "Full code, Helm chart, and Terraform in the repo."

## Cut for time

If you need to ship a 90-second cut for X/Threads/Bluesky:
- 0:00–0:15 — architecture diagram
- 0:15–1:00 — expanded agents (cost runaway) incident card on the dashboard
- 1:00–1:20 — Slack message with feedback buttons
- 1:20–1:30 — repo URL

## Watch out for

- Real GCP billing if you leave it running. `terraform destroy` from `deploy/terraform/` when you're done filming.
- Don't show your real Slack workspace name — use a throwaway `#ai-sre-demo` channel.
- The audit bucket has 30-day retention locked; you can't `terraform destroy` it inside that window. Set `retention_policy.is_locked = false` and `force_destroy = true` in `main.tf` if you're filming and tearing down repeatedly.
- The `inference-agents` cost spike is the moneymaker for the demo — that's the one that lands the "AI watching your AI" pitch. Make sure it's firing before you start recording.
