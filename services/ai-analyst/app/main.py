"""
AI SRE Analyst — main FastAPI entrypoint (v2.1).

Pipeline per alert:
    1. Dedup (Redis fingerprint TTL)
    2. Correlate (cross-namespace 90s window)
    3. Build incident timeline (events + deploys + logs in parallel)
    4. Cost-guard decides: summary_only / llm_no_rag / full_rag_llm
    5. Tier-1 deterministic rules over the timeline
    6. If no tier-1 hit: dispatch per cost-guard decision
       - summary_only: deterministic summary from the timeline
       - llm_no_rag:   Gemini Flash, short prompt, no RAG
       - full_rag_llm: RAG top-k=4 + Gemini Flash with full timeline
    7. Persist verdict to durable incident store (Firestore)
    8. Fan out evidence-cited verdict to Slack / dashboard / audit log

Feedback loop now goes through a review queue:
    POST /v1/feedback                  → submit lesson for review
    GET  /v1/lessons                   → reviewer sees pending queue
    POST /v1/lessons/{id}/approve      → approve + embed into Qdrant
    POST /v1/lessons/{id}/reject       → reject with reason
"""

import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import Counter, Histogram, make_asgi_app

from app.analyzer import IncidentAnalyzer
from app.audit import AuditLogger
from app.cost_guard import CostGuard, fallback_summary_from_timeline
from app.dedup import AlertDeduplicator
from app.incident_store import IncidentStore
from app.models import (
    AlertmanagerPayload,
    AnalysisResult,
    Evidence,
    FeedbackPayload,
)
from app.notifier import Notifier
from app.review_queue import ReviewQueue
from app.signals import DeploymentCollector, EventCollector, LogCollector
from app.timeline import TimelineBuilder
from app.vectorstore import RunbookStore

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
log = logging.getLogger("ai-analyst")

# --- Prometheus metrics ----------------------------------------------------
ALERTS_RECEIVED = Counter(
    "aianalyst_alerts_received_total",
    "Total alerts received from Alertmanager",
    ["namespace", "severity"],
)
ALERTS_DEDUPED = Counter(
    "aianalyst_alerts_deduped_total", "Alerts suppressed by dedup layer"
)
ANALYSES_RUN = Counter(
    "aianalyst_analyses_total", "Verdicts produced", ["tier", "dispatch"]
)
ANALYSIS_LATENCY = Histogram(
    "aianalyst_analysis_seconds",
    "End-to-end analysis latency (alert -> verdict)",
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60),
)
TIMELINE_EVENTS = Histogram(
    "aianalyst_timeline_events",
    "Number of events merged into the incident timeline",
    buckets=(0, 1, 2, 5, 10, 25, 50, 100),
)
LESSONS_SUBMITTED = Counter(
    "aianalyst_lessons_submitted_total", "Corrections submitted for review"
)
LESSONS_APPROVED = Counter(
    "aianalyst_lessons_approved_total", "Corrections approved into KB"
)


# --- Lifecycle -------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Booting AI SRE Analyst v2.1")
    project = os.environ["GCP_PROJECT"]

    app.state.dedup = AlertDeduplicator(redis_url=os.environ["REDIS_URL"])
    app.state.cost_guard = CostGuard(
        redis_url=os.environ["REDIS_URL"],
        daily_token_budget=int(os.getenv("DAILY_TOKEN_BUDGET", "1000000")),
    )
    app.state.runbooks = RunbookStore(
        qdrant_url=os.environ["QDRANT_URL"],
        collection=os.getenv("QDRANT_COLLECTION", "runbooks"),
    )
    app.state.analyzer = IncidentAnalyzer(
        project=project,
        location=os.getenv("GCP_LOCATION", "us-central1"),
        model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
    )
    app.state.timeline = TimelineBuilder(
        events=EventCollector(),
        deployments=DeploymentCollector(),
        logs=LogCollector(project=project),
    )
    app.state.notifier = Notifier(
        slack_webhook=os.getenv("SLACK_WEBHOOK_URL"),
        dashboard_url=os.getenv("DASHBOARD_INGEST_URL"),
    )
    app.state.audit = AuditLogger(
        bucket=os.environ["AUDIT_BUCKET"],
        project=project,
    )
    app.state.incidents = IncidentStore(project=project)
    app.state.lessons = ReviewQueue(project=project)

    await app.state.dedup.connect()
    await app.state.cost_guard.connect()
    log.info("Boot complete")
    yield
    await app.state.dedup.close()
    await app.state.cost_guard.close()


app = FastAPI(
    title="AI SRE Analyst",
    description="GenAI-powered incident analyst for LLM serving on Kubernetes.",
    version="2.1.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/metrics", make_asgi_app())


# --- Health ---------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# --- Alert webhook --------------------------------------------------------
@app.post("/v1/alerts", response_model=list[AnalysisResult])
async def receive_alerts(payload: AlertmanagerPayload, request: Request):
    """Webhook endpoint registered with Prometheus Alertmanager."""
    results: list[AnalysisResult] = []
    s = request.app.state

    for alert in payload.alerts:
        ns = alert.labels.get("namespace", "unknown")
        sev_label = alert.labels.get("severity", "warning")
        ALERTS_RECEIVED.labels(namespace=ns, severity=sev_label).inc()

        if await s.dedup.is_duplicate(alert):
            ALERTS_DEDUPED.inc()
            log.info("Suppressed duplicate alert %s", alert.fingerprint)
            continue

        correlated = await s.dedup.correlate(alert, window_seconds=90)

        start = time.monotonic()
        timeline = await s.timeline.build(alert)
        TIMELINE_EVENTS.observe(len(timeline.events))

        # Tier 1 — deterministic rules over the timeline. Free, fast.
        verdict = s.analyzer.try_tier1(alert, timeline)
        if verdict:
            ANALYSES_RUN.labels(tier="1_rule", dispatch="rules").inc()
            ANALYSIS_LATENCY.observe(time.monotonic() - start)
            await _publish(verdict, s)
            results.append(verdict)
            continue

        # Cost guard: severity-tiered dispatch.
        decision = await s.cost_guard.decide(sev_label)
        log.info(
            "Dispatch decision for %s: %s (%s)",
            alert.fingerprint, decision.tier, decision.reason,
        )

        if not decision.use_llm:
            verdict = _build_summary_only_verdict(alert, timeline, decision.reason)
            ANALYSES_RUN.labels(tier="1_rule", dispatch=decision.tier).inc()
            ANALYSIS_LATENCY.observe(time.monotonic() - start)
            await _publish(verdict, s)
            results.append(verdict)
            continue

        # LLM path. RAG only if cost guard says so.
        runbook_ctx = []
        if decision.use_rag:
            runbook_ctx = await s.runbooks.search(
                query=f"{alert.labels.get('alertname','')} {alert.annotations.get('summary','')}",
                top_k=4,
            )

        verdict = await s.analyzer.analyze(
            alert=alert,
            timeline=timeline,
            runbook_context=runbook_ctx,
            correlated_alert_ids=[c["fingerprint"] for c in correlated],
        )
        # Approximate token accounting — full accounting would read
        # response.usage_metadata; we estimate from prompt + output size.
        await s.cost_guard.record_tokens(
            in_tokens=_estimate_tokens(verdict.timeline),
            out_tokens=len(verdict.probable_cause) // 4
                       + sum(len(s) // 4 for s in verdict.remediation_steps),
        )
        ANALYSIS_LATENCY.observe(time.monotonic() - start)
        ANALYSES_RUN.labels(tier="3_llm", dispatch=decision.tier).inc()

        await _publish(verdict, s)
        results.append(verdict)

    return results


# --- Feedback / review queue ---------------------------------------------
@app.post("/v1/feedback")
async def feedback(payload: FeedbackPayload, request: Request):
    """SREs submit corrections. v2.1: corrections go to a review queue
    instead of straight into the KB. Approval is a separate endpoint."""
    s = request.app.state
    await s.incidents.record_feedback(payload)
    await s.audit.record_feedback(payload)

    if payload.was_correct is False and payload.correction:
        lesson_id = await s.lessons.submit(
            incident_id=payload.incident_id,
            sre_user=payload.sre_user,
            correction=payload.correction,
        )
        LESSONS_SUBMITTED.inc()
        return {"status": "submitted_for_review", "lesson_id": lesson_id}

    return {"status": "recorded"}


@app.get("/v1/lessons")
async def list_pending_lessons(request: Request, limit: int = 50):
    """List pending lessons awaiting reviewer approval."""
    return await request.app.state.lessons.list_pending(limit=limit)


@app.post("/v1/lessons/{lesson_id}/approve")
async def approve_lesson(lesson_id: str, request: Request, reviewer: str):
    """Approve a lesson and embed it into Qdrant under a curated source tag."""
    s = request.app.state
    lesson = await s.lessons.approve(lesson_id, reviewer)
    if not lesson:
        raise HTTPException(404, "lesson not found")

    await s.runbooks.upsert_lesson(
        incident_id=lesson["incident_id"],
        lesson=lesson["correction"],
        source_tag="feedback-loop:approved",
        submitted_by=lesson.get("submitted_by", "unknown"),
        approved_by=reviewer,
    )
    LESSONS_APPROVED.inc()
    log.info("Approved lesson %s by %s", lesson_id, reviewer)
    return {"status": "approved", "lesson_id": lesson_id}


@app.post("/v1/lessons/{lesson_id}/reject")
async def reject_lesson(lesson_id: str, request: Request, reviewer: str, reason: str):
    lesson = await request.app.state.lessons.reject(lesson_id, reviewer, reason)
    if not lesson:
        raise HTTPException(404, "lesson not found")
    return {"status": "rejected", "lesson_id": lesson_id}


# --- Incident introspection (used by the dashboard) ----------------------
@app.get("/v1/incidents")
async def list_incidents(request: Request, limit: int = 50):
    return await request.app.state.incidents.list_open(limit=limit)


@app.post("/v1/incidents/{incident_id}/resolve")
async def resolve_incident(incident_id: str, request: Request, sre_user: str):
    await request.app.state.incidents.resolve(incident_id, sre_user)
    return {"status": "resolved"}


# --- Helpers --------------------------------------------------------------
async def _publish(result: AnalysisResult, s) -> None:
    await s.incidents.open_incident(result)
    await s.notifier.fanout(result)
    await s.audit.record_analysis(result)
    log.info(
        "Verdict %s ns=%s tier=%s confidence=%.2f evidence=%d timeline=%d",
        result.incident_id,
        result.namespace,
        result.tier,
        result.confidence,
        len(result.evidence),
        len(result.timeline.events) if result.timeline else 0,
    )


def _build_summary_only_verdict(alert, timeline, reason: str) -> AnalysisResult:
    """Deterministic verdict for P3 alerts and budget-degraded paths."""
    cause, action, steps = fallback_summary_from_timeline(
        alert.annotations.get("summary", alert.labels.get("alertname", "alert")),
        timeline.events,
    )
    evidence = [
        Evidence(
            source="alert",
            ts=alert.startsAt,
            observation=f"Alert {alert.labels.get('alertname','?')} fired",
            weight=1.0,
        ),
    ]
    # Cite the most-recent two timeline events as supporting evidence.
    causal = sorted(
        timeline.causal_candidates(),
        key=lambda e: e.correlation_score,
        reverse=True,
    )[:2]
    for e in causal:
        evidence.append(
            Evidence(source=e.source, ts=e.ts, observation=e.summary, weight=0.8)
        )

    return AnalysisResult(
        incident_id=str(uuid.uuid4()),
        namespace=alert.labels.get("namespace", "unknown"),
        severity=alert.labels.get("severity", "info"),
        tier="1_rule",
        probable_cause=f"{cause} (deterministic summary — {reason})",
        confidence=0.5,
        evidence=evidence,
        recommended_action=action,
        remediation_steps=steps,
        blast_radius=[alert.labels.get("service", "unknown")],
        timeline=timeline,
        runbook_refs=[],
        correlated_alert_ids=[],
        generated_at=datetime.now(timezone.utc),
        model="rule-engine-v2.1",
    )


def _estimate_tokens(timeline) -> int:
    """Rough token estimate so the cost guard tracks budget without us
    needing to plumb usage_metadata through Vertex SDK."""
    if not timeline:
        return 500
    # ~4 chars per token, plus the system instruction overhead (~600).
    chars = sum(len(e.summary or "") + 80 for e in timeline.events)
    return 600 + chars // 4
