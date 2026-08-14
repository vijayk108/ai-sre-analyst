"""
Durable incident store.

Redis is fine for dedup/correlation TTL keys, but it is not a source
of truth — it loses everything on a flush, has no querying beyond
what we put in keys, and has no audit story. Real incidents need:

- a stable incident_id that survives pod restarts
- the alert(s) that triggered the incident
- every verdict version we produced (tier-1 then tier-3 if we
  re-analyzed, plus any re-runs after SRE corrections)
- the timeline as it was at verdict time
- human feedback events, with timestamp and SRE identity
- a status field (open / resolved / closed) that drives dashboard
  filtering

We use Firestore here rather than Cloud SQL / Postgres because:

- It's GCP-native, IAM-authenticated via Workload Identity (no
  password in a secret, no connection string drift).
- Schemaless fits "every verdict version is a slightly different
  shape" much better than a normalised SQL schema.
- For a portfolio project, the operational surface area is much
  smaller (no migrations, no pgbouncer, no schema review).

For local development without a Firestore emulator, set
``INCIDENT_STORE=memory`` and we keep things in-process.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from app.models import AnalysisResult, FeedbackPayload

log = logging.getLogger("ai-analyst.incident_store")

COLLECTION = "incidents"


class IncidentStore:
    """Append-only-ish: we update the same document but keep verdict history."""

    def __init__(self, project: str, mode: Optional[str] = None):
        self._project = project
        self._mode = mode or os.getenv("INCIDENT_STORE", "firestore")
        if self._mode == "firestore":
            from google.cloud import firestore  # imported lazily

            self._client = firestore.AsyncClient(project=project)
        else:
            self._client = None
            self._memory: dict[str, dict[str, Any]] = {}
            log.warning("IncidentStore running in memory mode — not durable")

    async def open_incident(self, result: AnalysisResult) -> None:
        """Create or update the incident document with this verdict."""
        doc = self._verdict_doc(result)
        if self._mode == "firestore":
            ref = self._client.collection(COLLECTION).document(result.incident_id)
            snap = await ref.get()
            if snap.exists:
                existing = snap.to_dict() or {}
                verdicts = existing.get("verdicts", [])
                verdicts.append(doc)
                await ref.update({
                    "verdicts": verdicts,
                    "last_verdict_at": doc["generated_at"],
                    "current_confidence": result.confidence,
                })
            else:
                await ref.set({
                    "incident_id": result.incident_id,
                    "namespace": result.namespace,
                    "severity": result.severity,
                    "status": "open",
                    "opened_at": doc["generated_at"],
                    "last_verdict_at": doc["generated_at"],
                    "current_confidence": result.confidence,
                    "verdicts": [doc],
                    "feedback": [],
                })
        else:
            inc = self._memory.setdefault(result.incident_id, {
                "incident_id": result.incident_id,
                "namespace": result.namespace,
                "severity": result.severity,
                "status": "open",
                "opened_at": doc["generated_at"],
                "verdicts": [],
                "feedback": [],
            })
            inc["verdicts"].append(doc)
            inc["last_verdict_at"] = doc["generated_at"]
            inc["current_confidence"] = result.confidence

    async def record_feedback(self, payload: FeedbackPayload) -> None:
        feedback = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "sre_user": payload.sre_user,
            "was_correct": payload.was_correct,
            "correction": payload.correction,
        }
        if self._mode == "firestore":
            ref = self._client.collection(COLLECTION).document(payload.incident_id)
            snap = await ref.get()
            if not snap.exists:
                log.warning("Feedback for unknown incident %s", payload.incident_id)
                return
            existing = snap.to_dict() or {}
            fb = existing.get("feedback", [])
            fb.append(feedback)
            await ref.update({"feedback": fb})
        else:
            inc = self._memory.get(payload.incident_id)
            if not inc:
                return
            inc["feedback"].append(feedback)

    async def resolve(self, incident_id: str, resolved_by: str) -> None:
        body = {
            "status": "resolved",
            "resolved_at": datetime.now(timezone.utc).isoformat(),
            "resolved_by": resolved_by,
        }
        if self._mode == "firestore":
            await self._client.collection(COLLECTION).document(incident_id).update(body)
        else:
            inc = self._memory.get(incident_id)
            if inc:
                inc.update(body)

    async def list_open(self, limit: int = 50) -> list[dict[str, Any]]:
        if self._mode == "firestore":
            from google.cloud.firestore import Query  # type: ignore

            q = (
                self._client.collection(COLLECTION)
                .where("status", "==", "open")
                .order_by("opened_at", direction=Query.DESCENDING)
                .limit(limit)
            )
            return [d.to_dict() async for d in q.stream()]
        else:
            opens = [i for i in self._memory.values() if i.get("status") == "open"]
            opens.sort(key=lambda i: i.get("opened_at", ""), reverse=True)
            return opens[:limit]

    @staticmethod
    def _verdict_doc(r: AnalysisResult) -> dict[str, Any]:
        # Slim version — full raw timeline + runbook excerpts stay in the
        # audit log. Here we keep just enough for the dashboard's
        # Timeline tab and Runbooks panel to render.
        timeline_events = []
        if r.timeline:
            for e in r.timeline.events:
                timeline_events.append({
                    "ts": e.ts.isoformat(),
                    "source": e.source,
                    "summary": e.summary,
                    "severity": e.severity,
                    "correlation_score": e.correlation_score,
                })
        return {
            "tier": r.tier,
            "model": r.model,
            "probable_cause": r.probable_cause,
            "confidence": r.confidence,
            "recommended_action": r.recommended_action,
            "remediation_steps": r.remediation_steps,
            "evidence_count": len(r.evidence),
            "evidence": [e.model_dump(mode="json") for e in r.evidence],
            "blast_radius": r.blast_radius,
            "timeline": timeline_events,
            "runbook_refs": [
                {"title": rb.title, "source": rb.source, "score": rb.score}
                for rb in r.runbook_refs
            ],
            "routing_recommendations": [
                rec.model_dump(mode="json") for rec in r.routing_recommendations
            ],
            "generated_at": r.generated_at.isoformat(),
        }
