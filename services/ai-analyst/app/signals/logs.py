"""
Pluggable log collector.

The rest of the pipeline consumes ``TimelineEvent`` objects and does
not care where they came from. Concrete backends live behind the
:class:`LogCollector` structural protocol so the analyst can run on
GCP (Cloud Logging), on-prem (Loki), or a stub for local dev — one
env-var flip, no downstream code changes.

Selection is driven by ``LOG_BACKEND``:

    LOG_BACKEND=cloud_logging   (default)  GCP_PROJECT required
    LOG_BACKEND=loki                       LOKI_URL required
    LOG_BACKEND=none                       returns [] for all queries

Both real backends TTL-cache per ``(namespace, window_minutes)`` for
60s. Log queries are bulky and correlated alerts share windows, so
the first alert warms the cache and the rest get instant hits.
Cache is process-local; a production version would back it with the
Redis we already deployed.

We filter to ERROR/CRITICAL severity by default to keep the prompt
small. The LLM doesn't need the entire INFO log spam, just the
shape of what went wrong.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Protocol

import httpx

from app.models import TimelineEvent

log = logging.getLogger("ai-analyst.signals.logs")

CACHE_TTL_SECONDS = 60
MAX_LOG_LINES = 25


class LogCollector(Protocol):
    """Structural type for anything that can fetch log signals for a namespace.

    Kept as a Protocol (not an ABC) so backends don't need a shared base class
    — anything that provides ``async collect(namespace, window_minutes) -> list[TimelineEvent]``
    satisfies the contract.
    """

    async def collect(self, namespace: str, window_minutes: int = 15) -> list[TimelineEvent]:
        ...


# --- Factory --------------------------------------------------------------
def make_log_collector(project: str | None = None) -> LogCollector:
    """Pick the concrete collector based on ``LOG_BACKEND``.

    Called from :mod:`app.main` during startup. Falls back to ``NoopLogCollector``
    if the chosen backend is unconfigured — the analyst still boots and does
    tier-1 rules; only log-derived timeline enrichment is degraded.
    """
    backend = os.getenv("LOG_BACKEND", "cloud_logging").lower()

    if backend == "loki":
        url = os.getenv("LOKI_URL")
        if not url:
            log.warning("LOG_BACKEND=loki but LOKI_URL is unset; log signal disabled")
            return NoopLogCollector()
        tenant = os.getenv("LOKI_TENANT")
        return LokiLogCollector(url=url, tenant=tenant)

    if backend == "none":
        return NoopLogCollector()

    # Default: Cloud Logging
    if not project:
        log.warning("LOG_BACKEND=cloud_logging but no GCP project; log signal disabled")
        return NoopLogCollector()
    return CloudLoggingLogCollector(project=project)


# --- Shared cache mixin ---------------------------------------------------
class _CachedCollector:
    """Per-namespace TTL cache shared between real backends."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, int], tuple[float, list[TimelineEvent]]] = {}
        self._cache_lock = asyncio.Lock()

    async def _cached(
        self, key: tuple[str, int], loader
    ) -> list[TimelineEvent]:
        now = time.monotonic()
        async with self._cache_lock:
            cached = self._cache.get(key)
            if cached and now - cached[0] < CACHE_TTL_SECONDS:
                return cached[1]
        events = await loader()
        async with self._cache_lock:
            self._cache[key] = (now, events)
        return events


# --- Backend: Cloud Logging (GCP) -----------------------------------------
class CloudLoggingLogCollector(_CachedCollector):
    def __init__(self, project: str):
        super().__init__()
        self._project = project
        self._client = None
        try:
            from google.cloud import logging as gcp_logging  # lazy import
            self._client = gcp_logging.Client(project=project)
            log.info("Cloud Logging client initialised for project=%s", project)
        except Exception as exc:  # noqa: BLE001
            log.warning("Cloud Logging client unavailable: %s", exc)

    async def collect(self, namespace: str, window_minutes: int = 15) -> list[TimelineEvent]:
        if self._client is None:
            return []
        return await self._cached(
            (namespace, window_minutes),
            lambda: asyncio.get_running_loop().run_in_executor(
                None, self._fetch, namespace, window_minutes
            ),
        )

    def _fetch(self, namespace: str, window_minutes: int) -> list[TimelineEvent]:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        filter_str = (
            'resource.type="k8s_container" '
            f'resource.labels.namespace_name="{namespace}" '
            f'severity>=ERROR '
            f'timestamp>="{cutoff.isoformat()}"'
        )
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

        events: list[TimelineEvent] = []
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

            sev = "critical" if str(entry.severity).upper() in (
                "CRITICAL", "ALERT", "EMERGENCY",
            ) else "error"
            labels = entry.resource.labels or {}
            container = labels.get("container_name", "")
            pod = labels.get("pod_name", "")
            events.append(TimelineEvent(
                ts=ts_utc, source="log",
                summary=f"{container}/{pod}: {msg}".strip(": "),
                severity=sev,
                detail={"container": container, "pod": pod},
            ))
        events.sort(key=lambda e: e.ts)
        log.debug("cloud-logging: fetched %d entries ns=%s", len(events), namespace)
        return events


# --- Backend: Loki (self-hosted / Grafana Cloud) --------------------------
class LokiLogCollector(_CachedCollector):
    """Query Loki via /loki/api/v1/query_range with a LogQL filter.

    Assumes a Promtail/Alloy/Vector agent is shipping container logs with
    at least a ``namespace`` label (kubernetes-monitoring defaults do this).
    We ask Loki for ERROR-severity lines in the window. If severity is not
    a label in your setup, adjust ``_build_query`` to grep the line body:
    ``{namespace="..."} |~ "(?i)error|critical|panic|fatal"``.
    """

    def __init__(self, url: str, tenant: str | None = None, timeout_s: float = 5.0):
        super().__init__()
        self._base = url.rstrip("/")
        headers = {"X-Scope-OrgID": tenant} if tenant else {}
        self._client = httpx.AsyncClient(timeout=timeout_s, headers=headers)
        log.info("Loki collector initialised url=%s tenant=%s", self._base, tenant or "-")

    async def collect(self, namespace: str, window_minutes: int = 15) -> list[TimelineEvent]:
        return await self._cached(
            (namespace, window_minutes),
            lambda: self._fetch(namespace, window_minutes),
        )

    @staticmethod
    def _build_query(namespace: str) -> str:
        # Prefer a severity label if you have structured logs; fall back to
        # a text match so this works on stock kubernetes-monitoring configs.
        return (
            f'{{namespace="{namespace}"}} '
            f'|~ "(?i)error|critical|panic|fatal|exception"'
        )

    async def _fetch(self, namespace: str, window_minutes: int) -> list[TimelineEvent]:
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(minutes=window_minutes)
        # Loki wants RFC3339 or ns-since-epoch. Nanoseconds are unambiguous.
        params = {
            "query": self._build_query(namespace),
            "start": str(int(start_dt.timestamp() * 1_000_000_000)),
            "end": str(int(end_dt.timestamp() * 1_000_000_000)),
            "limit": str(MAX_LOG_LINES),
            "direction": "backward",
        }
        try:
            r = await self._client.get(f"{self._base}/loki/api/v1/query_range", params=params)
            r.raise_for_status()
            data = r.json().get("data", {})
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Loki fetch failed for ns=%s: %s", namespace, exc)
            return []

        events: list[TimelineEvent] = []
        for stream in data.get("result", []):
            labels = stream.get("stream", {}) or {}
            container = labels.get("container", labels.get("container_name", ""))
            pod = labels.get("pod", labels.get("pod_name", ""))
            for ts_ns_str, line in stream.get("values", []):
                try:
                    ts_ns = int(ts_ns_str)
                except (TypeError, ValueError):
                    continue
                ts_utc = datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=timezone.utc)
                msg = (line or "")[:280]
                lower = msg.lower()
                sev = "critical" if ("critical" in lower or "panic" in lower or "fatal" in lower) else "error"
                events.append(TimelineEvent(
                    ts=ts_utc, source="log",
                    summary=f"{container}/{pod}: {msg}".strip(": "),
                    severity=sev,
                    detail={"container": container, "pod": pod},
                ))
        events.sort(key=lambda e: e.ts)
        log.debug("loki: fetched %d entries ns=%s", len(events), namespace)
        return events


# --- Backend: noop --------------------------------------------------------
class NoopLogCollector:
    """Returns nothing. Used when no log backend is configured; the analyst
    still runs, just without log-derived timeline enrichment."""

    async def collect(self, namespace: str, window_minutes: int = 15) -> list[TimelineEvent]:
        return []
