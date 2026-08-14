"""
Redis-backed dedup and correlation layer.

- Dedup: alert fingerprints get a TTL'd key. Repeats inside the window
  are suppressed.
- Correlate: every alert is also pushed onto a sorted set keyed by
  cluster, scored by epoch seconds. We pull recent neighbours so the
  LLM sees them as context.
"""

from __future__ import annotations

import logging
import time
from typing import Iterable

import redis.asyncio as redis

from app.models import Alert

log = logging.getLogger("ai-analyst.dedup")

DEDUP_TTL_SECONDS = 300         # collapse identical alerts for 5 min
CORRELATION_KEY = "alerts:recent"
CORRELATION_RETENTION = 600     # keep 10 min of history for correlation


class AlertDeduplicator:
    def __init__(self, redis_url: str):
        self._url = redis_url
        self._client: redis.Redis | None = None

    async def connect(self) -> None:
        self._client = redis.from_url(self._url, decode_responses=True)
        await self._client.ping()
        log.info("Connected to Redis at %s", self._url)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    async def is_duplicate(self, alert: Alert) -> bool:
        assert self._client
        key = f"alert:fp:{alert.fingerprint}"
        # SET NX returns True only if it was newly set => not a duplicate.
        was_new = await self._client.set(key, "1", ex=DEDUP_TTL_SECONDS, nx=True)
        if was_new:
            await self._record_for_correlation(alert)
        return not bool(was_new)

    async def _record_for_correlation(self, alert: Alert) -> None:
        assert self._client
        now = time.time()
        member = (
            f"{alert.fingerprint}|{alert.labels.get('namespace','?')}"
            f"|{alert.labels.get('alertname','?')}"
            f"|{alert.labels.get('severity','?')}"
        )
        await self._client.zadd(CORRELATION_KEY, {member: now})
        await self._client.zremrangebyscore(
            CORRELATION_KEY, 0, now - CORRELATION_RETENTION
        )

    async def correlate(
        self, alert: Alert, window_seconds: int = 90
    ) -> list[dict[str, str]]:
        """Return alerts that fired in the same window across all namespaces."""
        assert self._client
        now = time.time()
        members: Iterable[str] = await self._client.zrangebyscore(
            CORRELATION_KEY, now - window_seconds, now
        )
        out: list[dict[str, str]] = []
        for m in members:
            try:
                fp, ns, name, sev = m.split("|", 3)
            except ValueError:
                continue
            if fp == alert.fingerprint:
                continue
            out.append(
                {"fingerprint": fp, "namespace": ns, "alertname": name, "severity": sev}
            )
        return out
