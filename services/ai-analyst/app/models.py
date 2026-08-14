"""Pydantic models shared across the AI Analyst service.

The headline change in v2 is the verdict schema. v1 returned a loose
``root_cause`` string. v2 returns ``probable_cause`` plus an ordered
list of ``evidence`` rows, each citing where the observation came
from (alert / event / deploy / log / runbook). This is what makes
the difference between "AI summary" and "AI investigation".
"""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


# --- Inbound: Prometheus Alertmanager payload -----------------------------
class Alert(BaseModel):
    status: Literal["firing", "resolved"]
    labels: dict[str, str]
    annotations: dict[str, str]
    startsAt: datetime
    endsAt: Optional[datetime] = None
    fingerprint: str


class AlertmanagerPayload(BaseModel):
    version: str = "4"
    receiver: str
    status: Literal["firing", "resolved"]
    alerts: list[Alert]
    groupLabels: dict[str, str] = Field(default_factory=dict)
    commonLabels: dict[str, str] = Field(default_factory=dict)


# --- Signal collectors emit these ----------------------------------------
SignalSource = Literal["alert", "k8s_event", "deployment", "log", "metric"]


class TimelineEvent(BaseModel):
    """One row in the incident timeline. Time-sortable, source-tagged."""

    ts: datetime
    source: SignalSource
    summary: str                           # one-line human description
    severity: Literal["info", "warning", "error", "critical"] = "info"
    detail: dict[str, str] = Field(default_factory=dict)  # raw fields
    correlation_score: float = 0.0         # how relevant to current alert


class IncidentTimeline(BaseModel):
    """Chronological narrative of what happened, built per incident."""

    window_start: datetime
    window_end: datetime
    namespace: str
    primary_alert_fingerprint: str
    events: list[TimelineEvent]            # sorted ascending by ts

    def causal_candidates(self) -> list[TimelineEvent]:
        """Events that preceded the alert and could plausibly have caused it."""
        primary = next(
            (e for e in self.events if e.source == "alert"),
            None,
        )
        if not primary:
            return []
        return [e for e in self.events if e.ts < primary.ts and e.source != "alert"]


# --- RAG context ----------------------------------------------------------
class RunbookHit(BaseModel):
    title: str
    excerpt: str
    score: float
    source: str


# --- Evidence-cited verdict (the core schema upgrade) --------------------
class Evidence(BaseModel):
    """A single piece of cited evidence behind the AI's hypothesis."""

    source: SignalSource
    ts: Optional[datetime] = None
    observation: str                       # what was seen, in plain language
    weight: float = 1.0                    # how much this drove the verdict


class RoutingRecommendation(BaseModel):
    """A concrete config change that should bring the workload back to baseline.

    Used primarily for cost-aware verdicts (e.g. agents loop runaway →
    lower ``max_tokens``), but applicable to any tunable: concurrency,
    batch size, model variant. Lives alongside the prose
    ``recommended_action`` so a human (or, later, an auto-applier behind
    an approval gate) can act without re-parsing English.
    """

    parameter: str                         # e.g. "max_tokens", "max_concurrent_requests"
    current_value: Optional[str] = None    # observed or assumed current value (string so units flow through)
    recommended_value: str                 # what to change it to
    unit: Optional[str] = None             # "tokens", "requests", etc.
    reason: str                            # one-line why, traceable to evidence


class AnalysisResult(BaseModel):
    incident_id: str
    namespace: str
    severity: str                          # P1 / P2 / P3 / warning / critical
    tier: Literal["1_rule", "2_ml", "3_llm"]

    # The verdict — evidence-cited shape, not loose prose
    probable_cause: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[Evidence] = Field(default_factory=list)
    recommended_action: str
    remediation_steps: list[str] = Field(default_factory=list)
    routing_recommendations: list[RoutingRecommendation] = Field(default_factory=list)

    # Context the verdict relied on
    blast_radius: list[str] = Field(default_factory=list)
    timeline: Optional[IncidentTimeline] = None
    runbook_refs: list[RunbookHit] = Field(default_factory=list)
    correlated_alert_ids: list[str] = Field(default_factory=list)

    generated_at: datetime
    model: str


class FeedbackPayload(BaseModel):
    incident_id: str
    was_correct: bool
    correction: Optional[str] = None
    sre_user: str
