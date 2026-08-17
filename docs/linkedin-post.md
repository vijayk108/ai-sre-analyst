# LinkedIn post — variants

Three lengths. Medium is the safest default for a portfolio feed post.
The long one works as a LinkedIn article; the short one is the
X / Threads / Bluesky cross-post.

Repo URL is already filled in throughout: https://github.com/vijayk108/ai-sre-analyst.
Attach `docs/architecture.svg` (or a PNG export) to the medium and long
variants — LinkedIn rewards image attachments, and the diagram is the
most-clickable artifact.

---

## Short (X / Threads / Bluesky · ~60 words)

TTFT under KV cache pressure. Output tokens exploding in an agent
recursion loop. GPU pressure on a canary until batched decode
collapses. None of these show up on a normal Kubernetes dashboard.

I built an AI SRE analyst for this class of incident. Every LLM
claim must cite a specific timeline event. Structured routing diffs,
not prose. Shadow-confidence check on P1s.

→ https://github.com/vijayk108/ai-sre-analyst

---

## Medium (LinkedIn feed · ~340 words · the default)

TTFT degradation under KV cache pressure. Output tokens exploding in
an agent recursion loop. GPU memory ramping on a canary until batched
decode collapses. None of these show up on a normal Kubernetes
dashboard.

I built an AI SRE analyst for exactly this class of incident.
Alertmanager webhook → Redis dedup + 90s cross-namespace correlation
→ per-incident timeline merged in parallel from K8s Events, recent
Deployments, and Cloud Logs → cost-guarded dispatch to deterministic
tier-1 rules or Gemini 2.5 Flash with RAG over Qdrant runbooks →
structured evidence-cited verdict fanned out to Slack, a Next.js
dashboard, and a versioned GCS audit log. Median alert-to-verdict:
~1.4s.

Design bets I'll defend in code review:

**The LLM reasons about timelines, not isolated alerts.** Every claim
in `probable_cause` must cite a specific timeline event or runbook
chunk. Hallucinated SRE advice is out by construction — the JSON
schema forces it.

**Verdicts include structured routing recommendations.** Not "lower
max_tokens" as prose. `{parameter: "max_tokens", current_value:
"8192", recommended_value: "1024", reason: "output rate 8.7×
baseline"}` as a typed diff. Machines can act on it; humans review it
in one glance.

**Shadow-confidence check on P1s.** Gemini doesn't expose per-token
logprobs, so I can't measure verdict trust directly. Each P1 goes
through OpenAI gpt-4o-mini in parallel — its geometric-mean
per-token probability becomes a trust score on the incident. Under
40%, the verdict is flagged for human review before Slack fan-out.
~$0.0002 per P1 alert.

**Curated feedback, not auto-embed.** SRE corrections go to a review
queue, not straight into Qdrant. A 3am typo can't poison the KB.

Provider-neutral by design: Cloud Logging ↔ Loki is a one-env-var
flip (the collector sits behind a Protocol); Firestore ↔ Postgres
and Vertex AI ↔ Bedrock are file-level swaps.

Deployed to GKE Autopilot via Terraform + Helm.

Code, chart, and architecture diagram → https://github.com/vijayk108/ai-sre-analyst

Stack: GKE Autopilot · Vertex AI Gemini 2.5 Flash · OpenAI
gpt-4o-mini · Qdrant · Redis · Prometheus · FastAPI · Next.js · Helm
· Terraform.

---

## Long (LinkedIn article · ~600 words)

**An AI SRE analyst for LLM serving infrastructure — end to end on GCP**

LLM serving has failure modes that a generic Kubernetes dashboard is
blind to. TTFT climbs under KV cache pressure and customers feel a
frozen UI long before any latency alert fires. Output tokens explode
in an agent recursion loop and finance flags a $40k weekend bill on
Monday. GPU memory ramps quietly during a canary release until
batched decode collapses and takes chat down with it. None of these
are CPU spikes you'd page on with a normal SRE setup.

I built an analyst for this class of incident and shipped it to real
GKE Autopilot this week.

**The pipeline.** An alert arrives at a FastAPI webhook. Redis dedup
suppresses identical fingerprints for five minutes. Every alert also
drops onto a 90-second sliding ZSET so the analyst sees what else is
on fire cross-namespace (canary release → chat KV pressure → agents
retry storm; one root cause, three alerts). A cost guard decides
severity tier. Boring shapes — OOMKilled, CrashLoopBackOff,
ImagePullBackOff, FailedScheduling — get handled by deterministic
rules with no LLM call. Everything else goes to Gemini 2.5 Flash
with a top-4 RAG pull from Qdrant runbooks. Median alert-to-verdict:
~1.4 seconds.

**What makes the verdict useful.** Every claim in the JSON
`probable_cause` must cite a specific timeline event or runbook
chunk. Not "the deployment probably caused it" — "ReplicaSet
inference-66d9674f76 created at 18:39:12 (source: k8s-events,
weight: 0.9)". Hallucinated SRE advice is out by construction because
the model literally cannot reference evidence that wasn't in its
input.

**Routing recommendations, structured.** When the timeline has enough
signal, the verdict includes a typed config diff:
`{parameter: "max_tokens", current_value: "8192", recommended_value:
"1024", reason: "output rate is 8.7× baseline"}`. Machines can act
on it; humans review it at a glance. Beats the usual "consider tuning
max_tokens" prose that shows up in AI-summary tools.

**Trust score on P1s.** Gemini doesn't expose per-token logprobs, so
I can't measure how confident it really was. Every P1 verdict gets a
shadow inference through OpenAI gpt-4o-mini running in parallel — its
geometric-mean per-token probability becomes a trust score attached
to the incident. Under 40% and the verdict is flagged "human review
needed" before Slack fan-out. ~$0.0002 per P1 alert. This is what
makes AI-suggested remediation defensible as more than a demo.

**Curated feedback, not auto-embed.** When an SRE clicks "Correct
it" on the Slack card, the correction goes to a review queue, not
straight into Qdrant. A 3am typo can't poison the KB.

**Not GCP-only.** Cloud Logging is a one-env-var flip to Loki; the
log collector sits behind a `typing.Protocol` with three concrete
implementations (Cloud Logging, Loki, Noop). Firestore ↔ Postgres and
Vertex AI ↔ Bedrock or Azure OpenAI are file-level swaps — the
pipeline logic is provider-neutral by design. Terraform provisions
GKE Autopilot + IAM + Artifact Registry + a versioned audit bucket;
Helm installs the analyst, mock inference workloads, Qdrant, and
Prometheus.

**Not shipped yet:** OpenTelemetry span ingest into the timeline, and
a tier-2 ML classifier between rules and the LLM. Both are follow-ups;
the current cut is enough to talk about.

Code, Helm chart, Terraform, architecture diagram → https://github.com/vijayk108/ai-sre-analyst

Stack: GKE Autopilot · Vertex AI Gemini 2.5 Flash · OpenAI
gpt-4o-mini · Qdrant · Redis · Prometheus · FastAPI · Next.js · Helm
· Terraform.

---

## Posting tips

- **Ship the medium variant by default**, with the architecture image
  attached. LinkedIn rewards posts with images.
- **The long variant works as a LinkedIn article**, not a feed post —
  feed posts get truncated past ~3 lines.
- **The short variant** is for X / Threads / Bluesky. The specific
  symptoms (TTFT, KV cache, agent recursion) beat generic "AI
  watching AI" framing on those platforms.
- **Lead with the failure-mode list.** That's the differentiator from
  generic K8s/SRE portfolio pieces and the part recruiters in
  AI-infra roles will recognize.
- **Best window**: Tuesday–Thursday, 8–10am your timezone. LinkedIn's
  engineering audience is at their desk.
- **Consider adding one screenshot** alongside the architecture SVG:
  the expanded incident card with the RoutingBlock is the single most
  compelling visual you have.
- **Pin the post** to your profile while you're job-hunting. It's a
  better landing page than a résumé link.
