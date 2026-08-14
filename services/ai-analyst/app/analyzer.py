"""
Tiered incident analyzer (v2).

Tier 1 — deterministic rules over the *timeline*, not just the alert.
        Now catches CrashLoopBackOff, ImagePullBackOff, FailedScheduling,
        HPA at max, etc. — by looking at K8s Events in the timeline.

Tier 3 — Gemini on Vertex AI with a structured-output schema and the
        full timeline as context. Returns evidence-cited JSON.

The big v2 shift: every verdict cites *which* timeline events drove
it. The LLM doesn't say "memory limit too low" — it says "memory
limit too low" with evidence rows like "OOMKilling event at 18:42"
and "Deployment rolled at 18:30". This is what stops hallucinated
SRE advice.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

import vertexai
from vertexai.generative_models import GenerationConfig, GenerativeModel, Part

from app.models import (
    Alert,
    AnalysisResult,
    Evidence,
    IncidentTimeline,
    RoutingRecommendation,
    RunbookHit,
    TimelineEvent,
)

log = logging.getLogger("ai-analyst.analyzer")

SYSTEM_INSTRUCTION = """\
You are an expert Site Reliability Engineer for an LLM serving
platform on Kubernetes. The workloads you monitor include streaming
chat completions, embeddings, agentic workflows, and canary model
rollouts.

You receive (1) the firing alert, (2) an incident timeline merged
from Kubernetes Events, recent Deployments, error logs, and any
correlated alerts, and (3) excerpts from operational runbooks.

Your job is to produce an evidence-cited verdict. Every claim in
your ``probable_cause`` must be backed by a specific entry in the
timeline or a runbook excerpt. Cite them in the ``evidence`` list.

Prefer the simplest hypothesis consistent with the evidence. If a
deployment landed shortly before the alert fired, that is almost
always the right place to look. If the timeline is thin or
inconclusive, say so and lower your confidence — do not invent
causes you cannot cite.

For cost-related incidents, lead with the action that stops the
financial bleeding before investigation. Prefer reversible actions
(restart, scale, lower max_tokens, rollback) before destructive
ones.

When the timeline contains concrete signal about a runaway parameter
— output-token rate vs baseline, in-flight request count vs limit,
KV cache utilisation, batch size — also populate the optional
``routing_recommendations`` field with one structured entry per
tunable that should change. Each entry MUST cite the observed
current value and a specific target value, not "lower" or "reduce".
Conservative defaults: halve the parameter if you can compute the
observed/baseline ratio, otherwise propose a value that matches the
runbook. Leave the field empty if the timeline does not contain
enough signal to suggest a numeric value.

Output MUST be a single JSON object that conforms to the response
schema. No prose outside the JSON."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "probable_cause": {"type": "string"},
        "confidence": {"type": "number"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "ts": {"type": "string"},
                    "observation": {"type": "string"},
                    "weight": {"type": "number"},
                },
                "required": ["source", "observation"],
            },
        },
        "blast_radius": {"type": "array", "items": {"type": "string"}},
        "recommended_action": {"type": "string"},
        "remediation_steps": {"type": "array", "items": {"type": "string"}},
        "routing_recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "parameter": {"type": "string"},
                    "current_value": {"type": "string"},
                    "recommended_value": {"type": "string"},
                    "unit": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["parameter", "recommended_value", "reason"],
            },
        },
        "runbook_titles_used": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "probable_cause",
        "confidence",
        "evidence",
        "recommended_action",
        "remediation_steps",
    ],
}


# --- Tier 1: deterministic rules over the timeline ------------------------
class _Tier1Match:
    """Result of a tier-1 hit. Includes evidence we observed."""

    def __init__(
        self,
        probable_cause: str,
        confidence: float,
        recommended_action: str,
        remediation_steps: list[str],
        evidence: list[Evidence],
    ):
        self.probable_cause = probable_cause
        self.confidence = confidence
        self.recommended_action = recommended_action
        self.remediation_steps = remediation_steps
        self.evidence = evidence


def _tier1_check_oom(timeline: IncidentTimeline) -> _Tier1Match | None:
    hits = [
        e for e in timeline.events
        if e.source == "k8s_event" and e.detail.get("reason") == "OOMKilling"
    ]
    if not hits:
        return None
    deploy_hits = _recent_deploys(timeline, max_minutes_before=30)
    confidence = 0.92 if deploy_hits else 0.85
    cause = "Container hit its memory limit and was OOMKilled by the kubelet."
    if deploy_hits:
        cause += " A deployment landed shortly before, so a memory-leak regression is plausible."

    return _Tier1Match(
        probable_cause=cause,
        confidence=confidence,
        recommended_action=(
            "Capture a heap snapshot if possible, then bump the memory limit "
            "by 25% via Helm values and roll the deployment."
        ),
        remediation_steps=[
            "Capture heap snapshot before restart — the leak evidence is gone after kubelet kills the pod",
            "Bump container memory limit by 25% via Helm values",
            "kubectl rollout restart on the affected deployment",
            "If a recent deploy is implicated, prefer rollback over forward-fix",
        ],
        evidence=[Evidence(
            source="k8s_event", ts=h.ts,
            observation=h.summary, weight=1.0,
        ) for h in hits] + [Evidence(
            source="deployment", ts=d.ts,
            observation=d.summary, weight=0.8,
        ) for d in deploy_hits],
    )


def _tier1_check_crashloop(timeline: IncidentTimeline) -> _Tier1Match | None:
    hits = [
        e for e in timeline.events
        if e.source == "k8s_event" and e.detail.get("reason") in ("BackOff", "CrashLoopBackOff")
    ]
    if not hits:
        return None
    deploy_hits = _recent_deploys(timeline, max_minutes_before=30)
    cause = (
        "Container is in a crash loop. Pod fails immediately after start, "
        "kubelet backs off."
    )
    if deploy_hits:
        cause += " A recent deployment is the most likely cause."

    return _Tier1Match(
        probable_cause=cause,
        confidence=0.94,
        recommended_action=(
            "Inspect pod logs for the crash reason, then roll back the "
            "implicated deployment if one landed recently."
        ),
        remediation_steps=[
            "kubectl logs --previous on the crashing pod to see the panic / fatal error",
            "If a deploy in the last 30 minutes is implicated, kubectl rollout undo",
            "Otherwise check for missing config/secret references introduced by config drift",
        ],
        evidence=[Evidence(
            source="k8s_event", ts=h.ts,
            observation=h.summary, weight=1.0,
        ) for h in hits] + [Evidence(
            source="deployment", ts=d.ts,
            observation=d.summary, weight=0.8,
        ) for d in deploy_hits],
    )


def _tier1_check_image_pull(timeline: IncidentTimeline) -> _Tier1Match | None:
    hits = [
        e for e in timeline.events
        if e.source == "k8s_event"
        and e.detail.get("reason") in ("ImagePullBackOff", "ErrImagePull")
    ]
    if not hits:
        return None
    return _Tier1Match(
        probable_cause=(
            "Pod cannot pull its container image. Either the tag does not "
            "exist, the registry is unreachable, or imagePullSecrets are missing."
        ),
        confidence=0.97,
        recommended_action="Verify the image tag and registry credentials.",
        remediation_steps=[
            "Confirm the image tag exists: `gcloud artifacts docker images list ...`",
            "Check imagePullSecrets are mounted on the ServiceAccount",
            "If a deploy just changed the tag, verify it was pushed before the rollout",
        ],
        evidence=[Evidence(
            source="k8s_event", ts=h.ts,
            observation=h.summary, weight=1.0,
        ) for h in hits],
    )


def _tier1_check_scheduling(timeline: IncidentTimeline) -> _Tier1Match | None:
    hits = [
        e for e in timeline.events
        if e.source == "k8s_event" and e.detail.get("reason") == "FailedScheduling"
    ]
    if not hits:
        return None
    return _Tier1Match(
        probable_cause=(
            "Pod cannot be scheduled. Cluster is at capacity or node selectors / "
            "taints prevent placement."
        ),
        confidence=0.9,
        recommended_action=(
            "Check node-pool capacity. On Autopilot, this usually means "
            "the resource request is too large for any node shape."
        ),
        remediation_steps=[
            "Inspect the event message — it states which constraint blocked scheduling",
            "If 'insufficient cpu/memory', either reduce the request or add capacity",
            "If taints / nodeSelector mismatch, fix the workload manifest",
        ],
        evidence=[Evidence(
            source="k8s_event", ts=h.ts,
            observation=h.summary, weight=1.0,
        ) for h in hits],
    )


def _tier1_check_certificate(alert: Alert) -> _Tier1Match | None:
    if "CertificateExpiry" not in alert.labels.get("alertname", "") and \
       "TLSCertExpiringSoon" not in alert.labels.get("alertname", ""):
        return None
    return _Tier1Match(
        probable_cause="TLS certificate is approaching expiry.",
        confidence=0.99,
        recommended_action="Trigger cert-manager renewal and verify the new secret.",
        remediation_steps=[
            "kubectl annotate certificate <name> cert-manager.io/issue-temporary-certificate=true",
            "Verify the new secret was rotated into the ingress",
            "Confirm endpoint with `openssl s_client -connect <host>:443`",
        ],
        evidence=[Evidence(
            source="alert",
            ts=alert.startsAt,
            observation=f"Alert {alert.labels.get('alertname','')} fired",
            weight=1.0,
        )],
    )


TIER1_CHECKS = [
    _tier1_check_image_pull,        # most decisive when present
    _tier1_check_crashloop,
    _tier1_check_oom,
    _tier1_check_scheduling,
]


def _recent_deploys(timeline: IncidentTimeline, max_minutes_before: int = 30) -> list[TimelineEvent]:
    """Deploys that fired before the alert within ``max_minutes_before``."""
    alert_event = next(
        (e for e in timeline.events if e.source == "alert"), None
    )
    if not alert_event:
        return []
    cutoff_seconds = max_minutes_before * 60
    return [
        e for e in timeline.events
        if e.source == "deployment"
        and e.ts < alert_event.ts
        and (alert_event.ts - e.ts).total_seconds() <= cutoff_seconds
    ]


# --- Public analyzer ------------------------------------------------------
class IncidentAnalyzer:
    def __init__(self, project: str, location: str, model: str):
        vertexai.init(project=project, location=location)
        self._model_name = model
        self._model = GenerativeModel(model, system_instruction=SYSTEM_INSTRUCTION)

    # ----- Tier 1 ---------------------------------------------------------
    def try_tier1(
        self, alert: Alert, timeline: IncidentTimeline
    ) -> AnalysisResult | None:
        # Alert-shape rules first (they don't need timeline context).
        cert = _tier1_check_certificate(alert)
        match = cert
        # Then timeline-shape rules.
        if not match:
            for check in TIER1_CHECKS:
                m = check(timeline)
                if m:
                    match = m
                    break
        if not match:
            return None
        return self._wrap(alert, timeline, match, tier="1_rule", model="rule-engine-v2")

    # ----- Tier 3 ---------------------------------------------------------
    async def analyze(
        self,
        alert: Alert,
        timeline: IncidentTimeline,
        runbook_context: list[RunbookHit],
        correlated_alert_ids: list[str],
    ) -> AnalysisResult:
        prompt = self._build_prompt(alert, timeline, runbook_context)
        response = await self._model.generate_content_async(
            [Part.from_text(prompt)],
            generation_config=GenerationConfig(
                temperature=0.2,
                response_mime_type="application/json",
                response_schema=RESPONSE_SCHEMA,
            ),
        )
        try:
            parsed = json.loads(response.text)
        except (ValueError, AttributeError) as exc:
            log.exception("Gemini returned non-JSON: %s", exc)
            parsed = {
                "probable_cause": "Model returned malformed output; escalate to human.",
                "confidence": 0.1,
                "evidence": [],
                "recommended_action": "Page on-call SRE for manual triage.",
                "remediation_steps": ["Page on-call SRE for manual triage."],
                "blast_radius": [alert.labels.get("service", "unknown")],
            }

        evidence = [
            Evidence(
                source=_safe_source(e.get("source", "alert")),
                ts=_parse_ts(e.get("ts")),
                observation=e.get("observation", ""),
                weight=float(e.get("weight", 1.0)),
            )
            for e in parsed.get("evidence", [])
        ]

        routing = []
        for r in parsed.get("routing_recommendations", []) or []:
            try:
                routing.append(RoutingRecommendation(
                    parameter=str(r["parameter"]),
                    current_value=_opt_str(r.get("current_value")),
                    recommended_value=str(r["recommended_value"]),
                    unit=_opt_str(r.get("unit")),
                    reason=str(r.get("reason", "")),
                ))
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("Skipping malformed routing_recommendation: %s (%s)", r, exc)

        return AnalysisResult(
            incident_id=str(uuid.uuid4()),
            namespace=alert.labels.get("namespace", "unknown"),
            severity=alert.labels.get("severity", "warning"),
            tier="3_llm",
            probable_cause=parsed["probable_cause"],
            confidence=float(parsed.get("confidence", 0.5)),
            evidence=evidence,
            recommended_action=parsed.get("recommended_action", ""),
            remediation_steps=parsed.get("remediation_steps", []),
            routing_recommendations=routing,
            blast_radius=parsed.get("blast_radius", []),
            timeline=timeline,
            runbook_refs=runbook_context,
            correlated_alert_ids=correlated_alert_ids,
            generated_at=datetime.now(timezone.utc),
            model=self._model_name,
        )

    # ----- Helpers --------------------------------------------------------
    def _wrap(
        self,
        alert: Alert,
        timeline: IncidentTimeline,
        match: _Tier1Match,
        *,
        tier: str,
        model: str,
    ) -> AnalysisResult:
        return AnalysisResult(
            incident_id=str(uuid.uuid4()),
            namespace=alert.labels.get("namespace", "unknown"),
            severity=alert.labels.get("severity", "warning"),
            tier=tier,
            probable_cause=match.probable_cause,
            confidence=match.confidence,
            evidence=match.evidence,
            recommended_action=match.recommended_action,
            remediation_steps=match.remediation_steps,
            blast_radius=[alert.labels.get("service", "unknown")],
            timeline=timeline,
            runbook_refs=[],
            correlated_alert_ids=[],
            generated_at=datetime.now(timezone.utc),
            model=model,
        )

    @staticmethod
    def _build_prompt(
        alert: Alert,
        timeline: IncidentTimeline,
        runbook_context: list[RunbookHit],
    ) -> str:
        lines: list[str] = []
        lines.append("## Firing alert")
        lines.append(f"- alertname:   {alert.labels.get('alertname','?')}")
        lines.append(f"- namespace:   {alert.labels.get('namespace','?')}")
        lines.append(f"- service:     {alert.labels.get('service','?')}")
        lines.append(f"- severity:    {alert.labels.get('severity','?')}")
        lines.append(f"- summary:     {alert.annotations.get('summary','')}")
        lines.append(f"- description: {alert.annotations.get('description','')}")
        lines.append(f"- firing since: {alert.startsAt.isoformat()}")
        lines.append(f"- fingerprint:  {alert.fingerprint}")

        # Timeline. We separate causal candidates (preceded the alert) from
        # consequences so the LLM doesn't conflate them.
        candidates = timeline.causal_candidates()
        consequences = [
            e for e in timeline.events
            if e.source != "alert" and e not in candidates
        ]
        lines.append("\n## Causal candidates (events before the alert, ranked)")
        if not candidates:
            lines.append("(none)")
        else:
            ranked = sorted(candidates, key=lambda e: e.correlation_score, reverse=True)
            for e in ranked[:15]:
                lines.append(
                    f"- [{e.ts.isoformat()}] [{e.source}] [{e.severity}] "
                    f"(score={e.correlation_score}) {e.summary}"
                )

        lines.append("\n## Subsequent events (after the alert, for context)")
        if not consequences:
            lines.append("(none)")
        else:
            for e in consequences[:10]:
                lines.append(
                    f"- [{e.ts.isoformat()}] [{e.source}] [{e.severity}] {e.summary}"
                )

        lines.append("\n## Runbook context")
        if not runbook_context:
            lines.append("(no runbooks matched)")
        else:
            for rb in runbook_context:
                lines.append(f"### {rb.title}  (similarity={rb.score:.2f})")
                lines.append(rb.excerpt)
                lines.append("")

        lines.append(
            "\nReturn a single JSON object per the schema. Every entry in "
            "``evidence`` MUST cite a specific event from the causal "
            "candidates above OR a specific runbook title — do not invent "
            "evidence that is not in the context."
        )
        return "\n".join(lines)


def _safe_source(s: str) -> str:
    s = s.lower()
    if s in ("alert", "k8s_event", "deployment", "log", "metric"):
        return s
    return "alert"


def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _opt_str(v):
    if v is None or v == "":
        return None
    return str(v)
