"""Smoke test: AnalysisResult ↔ verdict_doc round-trip for the new
routing_recommendations field. Run with the analyst venv (only needs pydantic).

    python3 -m venv /tmp/sre-smoke-venv
    /tmp/sre-smoke-venv/bin/pip install pydantic==2.9.2
    /tmp/sre-smoke-venv/bin/python scripts/smoke_routing.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make `app.*` importable without setting PYTHONPATH externally.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "services" / "ai-analyst"))

from app.incident_store import IncidentStore
from app.models import (
    AnalysisResult,
    Evidence,
    IncidentTimeline,
    RoutingRecommendation,
    TimelineEvent,
)


def main() -> int:
    now = datetime.now(timezone.utc)
    timeline = IncidentTimeline(
        window_start=now,
        window_end=now,
        namespace="inference-agents",
        primary_alert_fingerprint="fp-test",
        events=[
            TimelineEvent(
                ts=now, source="metric",
                summary="tokens_out 8.7x baseline", severity="error",
                correlation_score=0.9,
            ),
            TimelineEvent(
                ts=now, source="alert",
                summary="InferenceCostSpike fired", severity="critical",
            ),
        ],
    )
    r = AnalysisResult(
        incident_id="smoke-1",
        namespace="inference-agents",
        severity="critical",
        tier="3_llm",
        probable_cause="Agent loop runaway after agents-runtime v0.42 rollout.",
        confidence=0.91,
        evidence=[Evidence(
            source="metric", ts=now,
            observation="tokens_out 8.7x baseline", weight=0.95,
        )],
        recommended_action="Lower max_tokens to cap cost; rollback agents-runtime.",
        remediation_steps=["lower max_tokens", "helm rollback agents-runtime"],
        routing_recommendations=[
            RoutingRecommendation(
                parameter="max_tokens",
                current_value="8192",
                recommended_value="1024",
                unit="tokens",
                reason="Output rate is 8.7x baseline; 8x reduction lands cost at baseline.",
            ),
        ],
        blast_radius=["inference-agents/inference"],
        timeline=timeline,
        runbook_refs=[],
        correlated_alert_ids=[],
        generated_at=now,
        model="gemini-2.5-flash",
    )

    doc = IncidentStore._verdict_doc(r)
    print(json.dumps(doc, indent=2))

    assert "routing_recommendations" in doc, "routing_recommendations missing from verdict_doc"
    assert len(doc["routing_recommendations"]) == 1
    rec = doc["routing_recommendations"][0]
    assert rec["parameter"] == "max_tokens"
    assert rec["recommended_value"] == "1024"
    assert rec["current_value"] == "8192"

    assert "timeline" in doc and len(doc["timeline"]) == 2, "timeline missing or wrong length"
    assert doc["timeline"][0]["source"] == "metric"

    print("\nOK: routing_recommendations + timeline round-trip cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
