"""
Cloud Logging collector — async-cached.

Cloud Logging queries are bulky (network) and lossy (logs are
sampled). We don't want every alert to block on a 600ms log fetch,
and we definitely don't want to spend the API quota re-fetching the
same window for ten correlated alerts.

Strategy: per-namespace TTL cache (60s). First incident in a window
warms it, the rest get instant hits. The cache key is
``(namespace, window_minutes)`` so widely-different windows don't
collide. Cache is process-local — fine for the demo since the
analyst Deployment has 2 replicas; a real production version would
back this with the Redis we already deployed.

We filter to ERROR/CRITICAL severity by default to keep the prompt
small. The LLM doesn't need the entire INFO log spam, just the
shape of what went wrong.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from app.models import TimelineEvent

log = logging.getLogger("ai-analyst.signals.logs")

CACHE_TTL_SECONDS = 60
MAX_LOG_LINES = 25


class LogCollector:
    def __init__(self, project: str | None = None):
        self._project = project
        self._cache: dict[tuple[str, int], tuple[float, list[TimelineEvent]]] = {}
        self._cache_lock = asyncio.Lock()
        self._client = None
        if project:
            try:
                from google.cloud import logging as gcp_logging  # lazy import
                self._client = gcp_logging.Client(project=project)
                log.info("Cloud Logging client initialised for project=%s", project)
            except Exception as exc:  # noqa: BLE001
                log.warning("Cloud Logging client unavailable: %s", exc)
                self._client = None

    async def collect(
        self, namespace: str, window_minutes: int = 15
    ) -> list[TimelineEvent]:
        """TTL-cached fetch of recent error logs for ``namespace``."""
        if self._client is None:
            return []

        key = (namespace, window_minutes)
        now = time.monotonic()
        async with self._cache_lock:
            cached = self._cache.get(key)
            if cached and now - cached[0] < CACHE_TTL_SECONDS:
                return cached[1]

        # Cache miss — fetch. Use a thread executor since the GCP client is
        # synchronous.
        events = await asyncio.get_running_loop().run_in_executor(
            None, self._fetch, namespace, window_minutes
        )

        async with self._cache_lock:
            self._cache[key] = (now, events)
        return events

    def _fetch(self, namespace: str, window_minutes: int) -> list[TimelineEvent]:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        cutoff_str = cutoff.isoformat()

        # GKE container logs land under k8s_container resource; we filter
        # by namespace label and severity. For a real deployment you'd
        # also filter by pod label so we don't pull logs from sidecars.
        filter_str = (
            'resource.type="k8s_container" '
            f'resource.labels.namespace_name="{namespace}" '
            f'severity>=ERROR '
            f'timestamp>="{cutoff_str}"'
        )

        events: list[TimelineEvent] = []
        try:
            entries = list(
                self._client.list_entries(
                    filter_=filter_str,
                    order_by="timestamp desc",
                    max_results=MAX_LOG_LINES,
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Cloud Logging fetch failed for ns=%s: %s", namespace, exc)
            return []

        for entry in entries:
            ts = entry.timestamp
            if ts is None:
                continue
            ts_utc = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
            payload = entry.payload
            if isinstance(payload, dict):
                msg = payload.get("message") or str(payload)[:280]
            else:
                msg = str(payload)[:280]

            sev = "error"
            if str(entry.severity).upper() in ("CRITICAL", "ALERT", "EMERGENCY"):
                sev = "critical"

            container = (entry.resource.labels or {}).get("container_name", "")
            pod = (entry.resource.labels or {}).get("pod_name", "")
            events.append(
                TimelineEvent(
                    ts=ts_utc,
                    source="log",
                    summary=f"{container}/{pod}: {msg}".strip(": "),
                    severity=sev,
                    detail={"container": container, "pod": pod},
                )
            )

        events.sort(key=lambda e: e.ts)
        log.debug("fetched %d log entries from ns=%s", len(events), namespace)
        return events
