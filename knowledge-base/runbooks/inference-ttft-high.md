# Inference TTFT Degradation

When p95 time-to-first-token (TTFT) on a streaming inference workload
climbs above 1 second sustained for more than two minutes, treat as
SEV-2. Streaming UX is highly sensitive to TTFT — users notice 800ms
delays as "frozen".

## Symptoms

- `inference_time_to_first_token_seconds` p95 > 1s
- `kv_cache_usage_ratio` climbing toward 1.0
- `inference_inflight` gauge climbing because requests are queueing
  for batched execution
- Customer-facing chat or completion endpoints feel sluggish to first
  token, even if total latency stays similar

## Most common root causes

1. **KV cache saturation.** Concurrent long-context requests fill the
   GPU's KV cache. New requests evict in-flight context, forcing
   recomputation. Check `kv_cache_usage_ratio` — if it's above 0.85,
   this is almost certainly the cause.
2. **Batching collapse.** The serving framework (vLLM, TGI, Triton)
   has dropped from continuous batching to one-by-one execution due to
   memory pressure or a configuration regression.
3. **Cold start storm.** A wave of pods restarted simultaneously
   (autoscaler flap, image pull saturation, node maintenance) and
   they're all loading model weights at once.
4. **Recent model rollout.** A larger model variant or different
   quantization pushed memory pressure across the threshold.
5. **GPU contention.** Another pod on the same node is consuming GPU
   compute. On GKE Autopilot with shared GPU nodes this is rare but
   possible with spot pods.

## Recommended remediation

1. Check `kv_cache_usage_ratio`. If >0.85, reduce
   `max_concurrent_requests` in the serving framework config and
   scale replicas to absorb the offered load.
2. Identify whether this is a recent rollout: `kubectl rollout history
   deployment/inference -n <namespace>`. If a release landed in the
   last 30 minutes, prefer rollback over forward-fix.
3. If batching collapsed, inspect serving framework logs for
   "decode-only" or "prefill-only" warnings. A restart with a known-
   good configuration usually clears it.
4. If multiple pods just restarted, do NOT scale up further during the
   cold-start window — you'll deepen the storm. Wait until existing
   pods are warm before scaling.
5. For sustained pressure that resists remediation, switch traffic to
   a smaller / quantized model variant via canary routing while the
   underlying issue is investigated.

## Escalation

If p95 TTFT stays above 2s for 5 minutes after remediation, page the
inference-platform on-call. Customer impact threshold for status page
update is p95 > 3s sustained for 3 minutes.
