# GPU Memory Pressure & CUDA OOM

GPU memory is the most common scarce resource in LLM serving. CUDA
OOMs cascade — once a pod hits OOM, the model has to be reloaded
from scratch, taking the pod out of rotation for tens of seconds.
On streaming workloads this looks like a partial outage.

## Symptoms

- `gpu_memory_pressure_ratio` climbing above 0.85 sustained
- 503 responses with "CUDA out of memory" in pod logs
- Pod restart count climbing
- TTFT degraded for surviving pods because they're absorbing the
  load of restarting pods

## Most common root causes

1. **KV cache leak.** The serving framework isn't releasing KV cache
   entries from completed requests. Common after a framework version
   bump. Check the framework's release notes for known leaks.
2. **Batch size too large for current memory headroom.** Memory
   budget that worked for the previous model variant doesn't work
   for the new one (more parameters, longer context).
3. **Model not unloading on rollover.** Blue/green model swap left
   both copies resident in GPU memory.
4. **Stuck long-context request.** A single 32k-token request
   consuming KV cache for the entire batch, starving everyone else.
5. **GPU memory fragmentation** after long uptime — even though
   logical free memory exists, no contiguous allocation is possible.

## Recommended remediation

1. **Capture nvidia-smi output BEFORE restarting** — once the pod
   restarts, the evidence is gone.
2. If KV cache leak suspected, restart pods one at a time
   (`kubectl rollout restart`) so the cluster never goes below
   minimum capacity.
3. If batch size is the cause, lower `max_batch_size` in the serving
   framework config via Helm values, then roll the deployment.
4. If model rollover is the cause, force a full pod replacement
   rather than relying on the framework's hot-swap.
5. After mitigation, file a ticket for the inference team to
   investigate. GPU OOM is rarely "just bump the limit" — there's
   usually a real leak or a config bug behind it.

## Escalation

If three or more pods OOM within 10 minutes, suspect a code-level
leak in a recent release. Page the model-serving team and consider
a chart-level rollback. If sustained, fail traffic over to a known-
good deployment in another region while debugging.
