"""
Mock LLM inference service.

Emits realistic AI-infra Prometheus metrics for the demo:

- inference_requests_total{status, model}
- inference_latency_seconds                       (full request)
- inference_time_to_first_token_seconds           (TTFT — only for streaming workloads)
- inference_tokens_in_total / _out_total
- inference_cost_usd_total                        (synthesized from token counts)
- gpu_memory_pressure_ratio                       (synthesized 0..1)
- kv_cache_usage_ratio                            (synthesized 0..1)
- inference_inflight                              (gauge)

Set INCIDENT_MODE to script demo failures:
  ttft_spike       — KV cache fills up, TTFT climbs from 200ms → 3s+
  cost_runaway     — output tokens explode (agent loop, missing max_tokens)
  gpu_pressure     — GPU memory ratio climbs toward 1.0 then OOMs
  error_burst      — intermittent backend failures
  none             — healthy control

The four namespaces in the demo each get a different mode so a single
screencap shows four distinct AI-infra incident shapes.
"""

from __future__ import annotations

import asyncio
import math
import os
import random
import time

from fastapi import FastAPI, HTTPException
from prometheus_client import Counter, Gauge, Histogram, make_asgi_app

SERVICE = os.getenv("SERVICE_NAME", "inference-chat")
MODEL = os.getenv("MODEL_NAME", "gemma-7b-it")
WORKLOAD = os.getenv("WORKLOAD_KIND", "chat")  # chat | embed | agents | canary
INCIDENT_MODE = os.getenv("INCIDENT_MODE", "none").lower()
INCIDENT_AT_SECONDS = int(os.getenv("INCIDENT_AT_SECONDS", "180"))

# Synthetic cost model — close enough for demo, configurable per workload.
COST_PER_1K_INPUT = float(os.getenv("COST_PER_1K_INPUT_USD", "0.0001"))
COST_PER_1K_OUTPUT = float(os.getenv("COST_PER_1K_OUTPUT_USD", "0.0004"))

# --- Metrics --------------------------------------------------------------
LBL = ["service", "model"]

requests_total = Counter(
    "inference_requests_total",
    "Total inference requests",
    LBL + ["status"],
)
inference_latency = Histogram(
    "inference_latency_seconds",
    "End-to-end inference latency (request to last token)",
    LBL,
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)
ttft_seconds = Histogram(
    "inference_time_to_first_token_seconds",
    "Time to first token (streaming workloads only)",
    LBL,
    buckets=(0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10),
)
tokens_in = Counter(
    "inference_tokens_in_total", "Prompt tokens consumed", LBL,
)
tokens_out = Counter(
    "inference_tokens_out_total", "Completion tokens generated", LBL,
)
cost_usd = Counter(
    "inference_cost_usd_total",
    "Synthesized USD cost of inference",
    LBL,
)
inflight = Gauge(
    "inference_inflight", "In-flight inference requests", LBL,
)
gpu_pressure = Gauge(
    "gpu_memory_pressure_ratio",
    "Synthetic GPU memory pressure (0..1)",
    LBL,
)
kv_cache_usage = Gauge(
    "kv_cache_usage_ratio",
    "Synthetic KV-cache fill ratio (0..1)",
    LBL,
)

app = FastAPI(title=f"mock-{SERVICE}")
app.mount("/metrics", make_asgi_app())

START = time.monotonic()


def _in_incident_window() -> bool:
    return (time.monotonic() - START) > INCIDENT_AT_SECONDS


def _seconds_into_incident() -> float:
    return max(0.0, (time.monotonic() - START) - INCIDENT_AT_SECONDS)


@app.get("/healthz")
async def healthz():
    if INCIDENT_MODE == "gpu_pressure" and _in_incident_window():
        if gpu_pressure.labels(service=SERVICE, model=MODEL)._value.get() > 0.97:
            raise HTTPException(503, "GPU OOM imminent")
    return {"status": "ok", "service": SERVICE, "model": MODEL, "workload": WORKLOAD}


@app.post("/generate")
async def generate(prompt_tokens: int = 256, max_tokens: int = 256):
    """Streaming-style completion endpoint with TTFT + total latency."""
    inflight.labels(service=SERVICE, model=MODEL).inc()
    try:
        # --- Baseline behaviour -----------------------------------------
        ttft = max(0.05, random.gauss(0.18, 0.04))
        per_tok = max(0.005, random.gauss(0.012, 0.003))
        out_tokens = random.randint(80, max_tokens)
        in_tokens = prompt_tokens

        # KV cache fills with concurrent inflight requests; embed workloads
        # don't really have a meaningful KV cache, so leave it 0.
        if WORKLOAD in ("chat", "agents", "canary"):
            kv = min(0.85, 0.15 + 0.04 * inflight.labels(service=SERVICE, model=MODEL)._value.get())
            kv_cache_usage.labels(service=SERVICE, model=MODEL).set(kv)

        # --- Scripted incidents -----------------------------------------
        if _in_incident_window():
            t = _seconds_into_incident()
            if INCIDENT_MODE == "ttft_spike":
                # Ramp TTFT upward; KV cache also pinned high.
                ramp = min(1.0, t / 60.0)
                ttft = random.gauss(0.18 + 2.6 * ramp, 0.4)
                kv_cache_usage.labels(service=SERVICE, model=MODEL).set(min(0.98, 0.3 + 0.7 * ramp))

            elif INCIDENT_MODE == "cost_runaway":
                # Output tokens explode — classic agent loop / missing max_tokens.
                # Some calls return 4-8x normal output.
                if random.random() < 0.45:
                    out_tokens = int(random.uniform(2000, 6000))

            elif INCIDENT_MODE == "gpu_pressure":
                cur = gpu_pressure.labels(service=SERVICE, model=MODEL)._value.get()
                # Climb ~0.6% per call.
                gpu_pressure.labels(service=SERVICE, model=MODEL).set(min(cur + 0.006, 1.0))
                if cur > 0.9 and random.random() < 0.3:
                    requests_total.labels(service=SERVICE, model=MODEL, status="failed").inc()
                    raise HTTPException(503, "CUDA out of memory")

            elif INCIDENT_MODE == "error_burst":
                if random.random() < 0.35:
                    requests_total.labels(service=SERVICE, model=MODEL, status="failed").inc()
                    raise HTTPException(500, "model server returned 500")

        # --- Simulate streaming -----------------------------------------
        ttft = max(ttft, 0.02)
        await asyncio.sleep(ttft)
        ttft_seconds.labels(service=SERVICE, model=MODEL).observe(ttft)

        # Compute total latency; cap actual sleep so a runaway demo doesn't
        # block forever, but record the realistic value in the histogram.
        total_latency = ttft + per_tok * out_tokens
        await asyncio.sleep(min(per_tok * out_tokens, 0.5))
        inference_latency.labels(service=SERVICE, model=MODEL).observe(total_latency)

        # Update token + cost counters
        tokens_in.labels(service=SERVICE, model=MODEL).inc(in_tokens)
        tokens_out.labels(service=SERVICE, model=MODEL).inc(out_tokens)
        cost = (in_tokens / 1000.0) * COST_PER_1K_INPUT + (out_tokens / 1000.0) * COST_PER_1K_OUTPUT
        cost_usd.labels(service=SERVICE, model=MODEL).inc(cost)

        requests_total.labels(service=SERVICE, model=MODEL, status="success").inc()
        return {
            "service": SERVICE,
            "model": MODEL,
            "ttft_ms": int(ttft * 1000),
            "tokens_out": out_tokens,
            "cost_usd": round(cost, 6),
        }
    finally:
        inflight.labels(service=SERVICE, model=MODEL).dec()


@app.on_event("startup")
async def _bootstrap_traffic():
    """Self-driven traffic so the demo doesn't need an external load gen."""

    base_rps = {"chat": 12, "embed": 30, "agents": 4, "canary": 6}.get(WORKLOAD, 10)

    async def _loop():
        while True:
            try:
                # Sinusoidal traffic pattern — looks more realistic in graphs.
                t = time.monotonic() - START
                rps = base_rps * (1.0 + 0.25 * math.sin(t / 30.0))
                for _ in range(int(rps)):
                    asyncio.create_task(_self_call())
                await asyncio.sleep(1)
            except Exception:
                await asyncio.sleep(1)

    async def _self_call():
        try:
            in_toks = random.randint(64, 512) if WORKLOAD != "embed" else random.randint(100, 800)
            max_toks = {"chat": 400, "agents": 800, "canary": 400, "embed": 1}.get(WORKLOAD, 256)
            await generate(prompt_tokens=in_toks, max_tokens=max_toks)
        except HTTPException:
            pass

    asyncio.create_task(_loop())
