# LinkedIn post — variants

Three lengths to choose from. The medium one is probably the safest default for a portfolio post; the long one is for if you want to publish it as a LinkedIn article. Replace `[YOUR-REPO]` with your GitHub URL before posting.

Attach `docs/architecture.svg` (or a PNG export of it) to whichever variant you pick.

---

## Short (the hook — 60 words)

v1 of my AI SRE analyst summarized alerts. v2 investigates incidents.

It now builds a per-incident timeline by merging Kubernetes Events, recent Deployments, and error logs in parallel — then asks Gemini to produce an evidence-cited verdict where every claim must reference a specific timeline event.

Hallucinated SRE advice, gone by construction.

→ [YOUR-REPO]

---

## Medium (the default — ~210 words)

Shipped v2 of my AI SRE analyst this week. v1 summarized alerts. v2 investigates incidents.

The unlock: the LLM no longer reasons about an isolated Prometheus alert. It reasons about a per-incident *timeline* — the alert plus everything that preceded it within the hour.

Per incident the analyst now collects, in parallel:
- Kubernetes Events (live, ~50ms — catches CrashLoopBackOff, ImagePullBackOff, FailedScheduling, OOMKill before tier-1 even needs to think)
- Recent Deployment + ReplicaSet rollouts (live — recent deploys are the highest-prior root cause for almost every non-trivial alert)
- Cloud Logging error entries (async, TTL-cached — bulky, so we don't block on them)

Those merge into one chronological view. Pre-alert events get scored by causal plausibility (recency × severity × source weight). The LLM gets a ranked haystack, not a wall of context.

The verdict schema changed too: `probable_cause`, `confidence`, and an `evidence` array where every claim must cite a specific timeline event or runbook title. Hallucinated SRE advice is out by construction — Gemini cannot reference evidence that wasn't in the input.

Stack: GKE Autopilot · Vertex AI Gemini 2.5 Flash · Qdrant · Redis · Prometheus · Kubernetes API · Cloud Logging · Helm · Terraform.

Repo + Helm chart + architecture diagram → [YOUR-REPO]

---

## Long (article-style — ~510 words)

**An AI SRE Analyst for LLM serving infrastructure — end to end on GCP**

I built a portfolio project this week that I'm actually proud of. The pitch: an AI that watches your AI. The design choices are interesting enough that I wanted to write them up rather than just drop a repo link.

**The premise.** LLM serving has failure modes that don't show up cleanly on a generic Kubernetes dashboard. Time-to-first-token degrades under KV cache pressure, and your customers feel "frozen" UI long before any latency alert fires. Output tokens explode in an agent loop, and the cost graph compounds quietly until someone notices a $40k bill on Monday morning. A canary model release inflates GPU memory just enough that batched decode collapses, taking the chat workload down with it. None of these are CPU spikes you'd page on with a normal SRE setup.

**The pipeline.** Alerts arrive at a FastAPI webhook. The first thing it does is dedup — identical fingerprints get suppressed for five minutes via Redis. Then it correlates: every alert is also dropped onto a sliding ZSET so the analyst sees what else is on fire across workloads in the last 90 seconds, which is how cross-namespace cascades get caught (canary release → chat KV pressure → agents retry storm; one root cause, three alerts). Boring shapes (`GPUMemoryPressure` heading to OOM, `KVCacheSaturated`) get handled by a tier-1 rule engine — no LLM call needed, no token spend, sub-second response.

For everything else, the analyst embeds the alert into a 768-dim vector with Vertex AI `text-embedding-005`, fetches the top-4 most relevant runbook chunks from Qdrant, and assembles a structured prompt for `gemini-2.5-flash`. Gemini is asked to return JSON conforming to a strict schema (`root_cause`, `confidence`, `blast_radius`, `remediation_steps`), so the consumers — Slack, the Next.js dashboard, the GCS audit log — are type-safe end-to-end and the system can fail closed if the model returns garbage.

The Slack message has `✓ Correct` / `✏ Correct it` buttons. When an SRE submits a correction, that text is embedded and upserted back into Qdrant. The next time a similar alert fires, the corrected guidance shows up as runbook context. Closed-loop learning, mechanically simple.

**Why GCP.** GKE Autopilot for compute (no node management, pay per pod), Vertex AI for the model surface, Workload Identity to keep static creds out of pods, GCS with versioning and retention-locking for the audit bucket so it's compliant for AI-driven action review. Terraform provisions all of it; Helm installs the chart.

**The demo.** Four heterogeneous mock inference workloads run with scripted failure modes — TTFT spike on chat, cost runaway on agents, GPU pressure on canary, and a healthy embeddings workload as control. Three minutes after install they start misbehaving on schedule, so a screen recording reproduces predictably without depending on real traffic.

**What I'd build next.** A tier-2 ML classifier between the rules and the LLM. Cost-aware routing recommendations (when output rate spikes, suggest concrete `max_tokens` changes that would land you back at baseline). Auto-remediation with human approval, gated by org policy.

Stack: GKE Autopilot · Vertex AI Gemini 2.5 Flash · Qdrant · Redis · Prometheus · FastAPI · Next.js · Helm · Terraform.

Code, chart, and architecture diagram → [YOUR-REPO]

#GenAI #LLMOps #AIInfra #Kubernetes #SRE #GCP #Observability

---

## Tips

- Ship the **medium** variant by default with the architecture image attached. LinkedIn rewards posts with images, and the architecture diagram is the most clickable artifact you have.
- The **long** variant works as a LinkedIn article (the long-form publishing surface), not as a feed post — feed posts get truncated past ~3 lines.
- The **short** variant is for the X/Threads/Bluesky cross-post if you want one. The "AI watching your AI" hook outperforms the literal description in casual feeds.
- Lead the medium and long variants with the failure-mode list (TTFT, cost runaway, GPU pressure). That's the differentiator from generic K8s/SRE portfolio pieces and the part recruiters in AI-infra roles will recognize.
- Pin the post to your profile while you're job hunting. It's a better landing page than a résumé link.
