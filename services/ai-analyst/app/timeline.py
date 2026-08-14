"""
Incident timeline builder.

Takes the firing alert, calls all three signal collectors in
parallel, and merges the results into a single time-sorted
``IncidentTimeline``. This is the core upgrade in v2 — the LLM no
longer reasons about an isolated alert; it reasons about a narrative.

The causality assignment is intentionally simple:

- Anything before the alert with high severity = candidate cause
- Anything after the alert = consequence
- Recency wins ties (T-2m beats T-12m if both look plausible)

The LLM still does the actual reasoning. We just give it the
right haystack.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.models import (
    Alert,
    IncidentTimeline,
    TimelineEvent,
)
from app.signals import DeploymentCollector, EventCollector, LogCollector

log = logging.getLogger("ai-analyst.timeline")

# How far back to look. Deploys can cause delayed failures (memory
# leaks take 20+ minutes to manifest), so the deploy window is wider
# than the events window.
EVENT_WINDOW_MINUTES = 30
DEPLOY_WINDOW_MINUTES = 60
LOG_WINDOW_MINUTES = 15


class TimelineBuilder:
    def __init__(
        self,
        events: EventCollector,
        deployments: DeploymentCollector,
        logs: LogCollector,
    ):
        self._events = events
        self._deployments = deployments
        self._logs = logs

    async def build(self, alert: Alert) -> IncidentTimeline:
        namespace = alert.labels.get("namespace", "default")
        alert_ts = (
            alert.startsAt
            if alert.startsAt.tzinfo
            else alert.startsAt.replace(tzinfo=timezone.utc)
        )

        # Hybrid collection: events + deploys live (fast), logs async-cached.
        # Run all three concurrently so total wall time = max(slowest).
        events_t, deploys_t, logs_t = await asyncio.gather(
            self._events.collect(namespace, window_minutes=EVENT_WINDOW_MINUTES),
            self._deployments.collect(namespace, window_minutes=DEPLOY_WINDOW_MINUTES),
            self._logs.collect(namespace, window_minutes=LOG_WINDOW_MINUTES),
            return_exceptions=True,
        )

        merged: list[TimelineEvent] = []
        for collected in (events_t, deploys_t, logs_t):
            if isinstance(collected, Exception):
                log.warning("Signal collector raised: %s", collected)
                continue
            merged.extend(collected)

        # Add the alert itself as a timeline event so the LLM sees where
        # in the narrative it sits.
        merged.append(
            TimelineEvent(
                ts=alert_ts,
                source="alert",
                summary=(
                    f"Alert {alert.labels.get('alertname','?')} fired: "
                    f"{alert.annotations.get('summary','')}"
                ),
                severity=_severity_to_level(alert.labels.get("severity", "warning")),
                detail={
                    "alertname": alert.labels.get("alertname", ""),
                    "service": alert.labels.get("service", ""),
                    "fingerprint": alert.fingerprint,
                },
            )
        )

        # Score each non-alert event by causal plausibility relative to
        # the alert.
        for ev in merged:
            if ev.source == "alert":
                continue
            ev.correlation_score = _causality_score(ev, alert_ts)

        merged.sort(key=lambda e: e.ts)

        window_start = min((e.ts for e in merged), default=alert_ts)
        window_end = max((e.ts for e in merged), default=alert_ts)

        return IncidentTimeline(
            window_start=window_start,
            window_end=window_end,
            namespace=namespace,
            primary_alert_fingerprint=alert.fingerprint,
            events=merged,
        )


def _causality_score(event: TimelineEvent, alert_ts: datetime) -> float:
    """Heuristic: how likely is this event the cause of the alert?

    Higher = stronger candidate. Score combines:
      - temporal proximity (recent precedence beats distant)
      - severity weight
      - source weight (deploys > events > logs, on average)
    """
    if event.ts >= alert_ts:
        return 0.0  # consequence, not cause

    seconds_before = (alert_ts - event.ts).total_seconds()
    if seconds_before > 60 * 60:
        return 0.0
    # Linear decay over an hour.
    proximity = max(0.0, 1.0 - seconds_before / 3600.0)

    severity_weight = {
        "info": 0.3,
        "warning": 0.6,
        "error": 0.9,
        "critical": 1.0,
    }.get(event.severity, 0.5)

    source_weight = {
        "deployment": 1.0,    # rollouts are the most common root cause
        "k8s_event": 0.8,
        "log": 0.6,
        "metric": 0.5,
    }.get(event.source, 0.5)

    return round(proximity * severity_weight * source_weight, 3)


def _severity_to_level(sev: str) -> str:
    sev = sev.lower()
    if sev in ("critical", "p1"):
        return "critical"
    if sev in ("warning", "p2"):
        return "warning"
    if sev in ("error",):
        return "error"
    return "info"
