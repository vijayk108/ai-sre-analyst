"""
Append-only audit log.

Every AI verdict and SRE feedback event is written as a JSON line to a
GCS bucket configured with object versioning + retention policy, which
gives us tamper-resistant audit history compliant with PCI-DSS / SOC2
review requirements.

For local dev, if AUDIT_BUCKET is set to a path starting with
``file://`` we write to the local filesystem instead.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from app.models import AnalysisResult, FeedbackPayload

log = logging.getLogger("ai-analyst.audit")


class AuditLogger:
    def __init__(self, bucket: str, project: str):
        self._bucket = bucket
        self._project = project
        self._local = bucket.startswith("file://")
        if self._local:
            self._dir = bucket.removeprefix("file://")
            os.makedirs(self._dir, exist_ok=True)
            self._client = None
        else:
            from google.cloud import storage  # imported lazily

            self._client = storage.Client(project=project)
            self._gcs_bucket = self._client.bucket(bucket)

    async def record_analysis(self, r: AnalysisResult) -> None:
        await self._write("analysis", r.model_dump(mode="json"))

    async def record_feedback(self, f: FeedbackPayload) -> None:
        await self._write("feedback", f.model_dump(mode="json"))

    async def _write(self, kind: str, payload: dict) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        name = f"{kind}/{ts}.json"
        body = json.dumps({"kind": kind, "ts": ts, "payload": payload})
        if self._local:
            path = os.path.join(self._dir, name.replace("/", "_"))
            with open(path, "w") as fh:
                fh.write(body)
        else:
            blob = self._gcs_bucket.blob(name)
            blob.upload_from_string(body, content_type="application/json")
