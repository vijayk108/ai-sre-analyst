"""
SRE-feedback review queue.

The v1 design was naive: any SRE correction got embedded straight
back into Qdrant. That's how a tired SRE typing nonsense at 3am
poisons your knowledge base. v2 enforces a curation step.

Flow:
    SRE clicks "Correct it" in Slack
        → POST /v1/feedback with their correction text
        → ReviewQueue.submit() creates a `pending_lessons` doc
        → status: pending
    Reviewer (the team's senior SRE / inference platform lead) checks
    the queue periodically:
        → POST /v1/lessons/{id}/approve  → status: approved
        → POST /v1/lessons/{id}/reject   → status: rejected
    On approval, the lesson is embedded into Qdrant with provenance:
        - source: "feedback-loop:approved"
        - submitted_by: <sre_user>
        - approved_by: <reviewer>

We never auto-embed. The whole point is that the KB is curated.
The Slack correction button is now "submit for review", not
"upsert".
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

log = logging.getLogger("ai-analyst.review_queue")

LESSONS_COLLECTION = "pending_lessons"
LessonStatus = Literal["pending", "approved", "rejected"]


class ReviewQueue:
    def __init__(self, project: str, mode: Optional[str] = None):
        self._project = project
        self._mode = mode or os.getenv("INCIDENT_STORE", "firestore")
        if self._mode == "firestore":
            from google.cloud import firestore  # imported lazily

            self._client = firestore.AsyncClient(project=project)
        else:
            self._client = None
            self._memory: dict[str, dict[str, Any]] = {}

    async def submit(
        self,
        *,
        incident_id: str,
        sre_user: str,
        correction: str,
    ) -> str:
        """Add a correction to the queue. Returns the lesson_id."""
        lesson_id = str(uuid.uuid4())
        doc = {
            "lesson_id": lesson_id,
            "incident_id": incident_id,
            "submitted_by": sre_user,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "correction": correction,
            "status": "pending",
            "approved_by": None,
            "approved_at": None,
            "rejection_reason": None,
        }
        if self._mode == "firestore":
            await self._client.collection(LESSONS_COLLECTION).document(lesson_id).set(doc)
        else:
            self._memory[lesson_id] = doc
        log.info("Submitted lesson %s for review (incident=%s)", lesson_id, incident_id)
        return lesson_id

    async def approve(
        self, lesson_id: str, reviewer: str
    ) -> Optional[dict[str, Any]]:
        """Mark approved and return the lesson doc so the caller can embed it."""
        body = {
            "status": "approved",
            "approved_by": reviewer,
            "approved_at": datetime.now(timezone.utc).isoformat(),
        }
        if self._mode == "firestore":
            ref = self._client.collection(LESSONS_COLLECTION).document(lesson_id)
            snap = await ref.get()
            if not snap.exists:
                return None
            await ref.update(body)
            updated = snap.to_dict() or {}
            updated.update(body)
            return updated
        else:
            lesson = self._memory.get(lesson_id)
            if not lesson:
                return None
            lesson.update(body)
            return lesson

    async def reject(
        self, lesson_id: str, reviewer: str, reason: str
    ) -> Optional[dict[str, Any]]:
        body = {
            "status": "rejected",
            "approved_by": reviewer,
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "rejection_reason": reason,
        }
        if self._mode == "firestore":
            ref = self._client.collection(LESSONS_COLLECTION).document(lesson_id)
            snap = await ref.get()
            if not snap.exists:
                return None
            await ref.update(body)
            updated = snap.to_dict() or {}
            updated.update(body)
            return updated
        else:
            lesson = self._memory.get(lesson_id)
            if not lesson:
                return None
            lesson.update(body)
            return lesson

    async def list_pending(self, limit: int = 50) -> list[dict[str, Any]]:
        if self._mode == "firestore":
            from google.cloud.firestore import Query  # type: ignore

            q = (
                self._client.collection(LESSONS_COLLECTION)
                .where("status", "==", "pending")
                .order_by("submitted_at", direction=Query.DESCENDING)
                .limit(limit)
            )
            return [d.to_dict() async for d in q.stream()]
        else:
            pending = [
                l for l in self._memory.values() if l.get("status") == "pending"
            ]
            pending.sort(key=lambda l: l.get("submitted_at", ""), reverse=True)
            return pending[:limit]
