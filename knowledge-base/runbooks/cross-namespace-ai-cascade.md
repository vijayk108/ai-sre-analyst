# Cross-Namespace AI Cascade

When alerts fire simultaneously across two or more inference
workloads (chat, embed, agents, canary) within a 90-second window,
suspect a shared dependency rather than four independent failures.
AI workloads share more infrastructure than traditional
microservices: GPU node pools, model registry, embedding caches,
and rate-limited upstream providers.

## Symptoms

- Same alert (or symptomatically related) firing in 2+ inference
  namespaces within 90 seconds
- Shared infra signals degraded: model registry latency, GPU node
  allocation failures, vector store query times
- Service mesh (Istio) reports degraded `destination_rule` health
  for shared model-serving routes

## Most common root causes

1. **Canary model release breaking dependents.** A new model
   variant rolls out in `inference-canary`. The agents workload,
   which calls the canary for some requests, sees latency spike
   → triggers retries → retries flood `inference-chat` (the
   fallback) → chat's KV cache saturates. One root cause, three
   alerts.
2. **Shared GPU node pool exhausted.** All four workloads autoscale
   into the same node pool. A traffic spike in one (or a leaked pod
   in another) consumes the available GPU capacity, so the others
   can't schedule new replicas when their HPAs request them.
3. **Vector store / cache outage.** RAG-style chat workloads share
   a Qdrant or Vertex AI Vector Search instance. If it degrades,
   chat and agents both surface latency at the same time, looking
   like correlated incidents.
4. **Shared embedding service.** Chat and agents both call the
   embeddings workload for retrieval. A latency spike in
   `inference-embed` cascades upstream into both consumers.
5. **Upstream provider rate-limit.** All workloads sharing a single
   Vertex AI quota or OpenAI key get throttled simultaneously.

## Recommended remediation

1. **Do NOT roll the most-symptomatic workload first.** It's
   probably the loudest victim, not the root cause. Identify the
   shared dependency before acting.
2. Overlay alert timelines. The shared-dep alert (if observable)
   precedes the namespace alerts by 30–60 seconds. The earliest
   symptom is the most likely root cause.
3. If a canary release is implicated, roll it back BEFORE touching
   the consumer workloads. Their symptoms will clear on their own.
4. If GPU node pool is exhausted, shed load on the lowest-priority
   workload (typically `inference-canary`) by reducing its HPA max
   to free capacity for production traffic.
5. If a shared vector store / embeddings service is the cause,
   serve cached responses with a degradation banner rather than
   propagating the failure.

## Escalation

A cross-namespace AI event is at minimum a SEV-2. Three or more
workloads affected → SEV-1, the blast radius covers all
GenAI-backed customer flows. If the canary is implicated and the
release was outside business hours, pull in the model-deploy
on-call before continuing.
