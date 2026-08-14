# Inference Cost Runaway

Triggers when output token rate exceeds 2x the rolling baseline,
indicating that completions are getting longer, not that traffic
went up. Cost compounds quickly — a 5x output explosion at $0.0004
per 1k tokens against 100 RPS is roughly $7 per minute extra,
$10k/day if undetected.

## Symptoms

- `inference_tokens_out_total` rate well above the 2-hour baseline
- `inference_cost_usd_total` slope steepens visibly
- Latency tail elongates (longer completions take longer)
- p99 of `inference_tokens_out` per-request distribution jumps
- No corresponding spike in request volume — same RPS, more output

## Most common root causes

1. **Agent loop.** An agentic workflow (typically the agents
   namespace) entered a self-referential reasoning loop where each
   thought generates more thoughts. Look for a recent prompt-template
   change in the orchestration code.
2. **Missing or relaxed `max_tokens`.** A deploy removed the
   per-request token cap, or raised it from 512 → 4096 thinking it
   was "safe defaults". Cross-reference the alert with the last
   deploy timestamp.
3. **Prompt injection.** Adversarial input pushing the model into
   verbose modes ("think step by step in detail and then ..."). More
   common on customer-facing chat than on internal agents.
4. **Model rollout.** Newer model variants (especially reasoning-
   tuned ones) emit longer outputs by default. A canary that hasn't
   had its `max_tokens` tuned will look like a cost regression.
5. **Repetition / degeneration.** Sampling parameters wrong
   (`repetition_penalty=1.0`, `temperature=0`) cause the model to
   loop on the same phrase until `max_tokens` is hit.

## Recommended remediation

1. **Cap first, investigate second.** If the cost trajectory is
   steep, lower `max_tokens` immediately on the affected workload to
   stop the bleeding. This is reversible.
2. Identify the cause class: agent loop vs config regression vs
   model regression. Recent deploys are the highest-prior suspect.
3. If a deploy is implicated, roll back. Token cost is real money;
   forward-fixing a $5k/hr leak is not the right call.
4. For agent loops specifically, add a recursion-depth guard or a
   token-budget guard at the orchestration layer, not just at the
   model call. Per-call caps don't catch loops.
5. Review sampling parameters. `repetition_penalty=1.05` and
   `temperature>0` together prevent the simplest degeneration loops.

## Escalation

If sustained cost rate exceeds $1/min above baseline, page
finance-aware on-call. If the workload is customer-billed, pause it
and serve a cached response with an apology rather than continue
emitting tokens against an unknown root cause.
