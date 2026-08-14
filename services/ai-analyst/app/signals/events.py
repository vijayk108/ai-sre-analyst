"""
Kubernetes Events collector.

K8s Events are the underused goldmine of incident context. They tell
you *why* a pod restarted, not just that it did:

- Killing: Container failed liveness probe
- BackOff: Back-off restarting failed container
- FailedScheduling: 0/4 nodes available (insufficient cpu)
- ImagePullBackOff: Failed to pull image
- Unhealthy: Readiness probe failed: HTTP probe failed with statuscode: 503

We pull these live per incident — they're cheap, the K8s API is
already in-cluster, and freshness matters. Latency target: <100ms.

Auth: in-cluster ServiceAccount token, scoped read-only via the
ai-sre-analyst RBAC role (Phase 2 hardening).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from app.models import TimelineEvent

log = logging.getLogger("ai-analyst.signals.events")

# K8s event reasons that almost always indicate a real problem.
HIGH_SIGNAL_REASONS = {
    "OOMKilling": "critical",
    "Killing": "warning",
    "BackOff": "error",
    "CrashLoopBackOff": "critical",
    "FailedScheduling": "error",
    "FailedMount": "error",
    "FailedAttachVolume": "error",
    "FailedCreate": "error",
    "ImagePullBackOff": "error",
    "ErrImagePull": "error",
    "Unhealthy": "warning",
    "FailedKillPod": "error",
    "Evicted": "warning",
    "NodeNotReady": "critical",
    "NodeHasDiskPressure": "warning",
    "NodeHasMemoryPressure": "warning",
    "ExceededGracePeriod": "warning",
    "FailedScale": "warning",          # HPA can't scale further
    "ScalingReplicaSet": "info",       # rollout in progress
    "Rebalanced": "info",
    "Created": "info",
    "Started": "info",
    "Pulled": "info",
}


class EventCollector:
    def __init__(self):
        try:
            # When the analyst itself runs in the cluster
            config.load_incluster_config()
            log.info("k8s client: using in-cluster config")
        except config.ConfigException:
            # Local dev — uses ~/.kube/config
            try:
                config.load_kube_config()
                log.info("k8s client: using kubeconfig")
            except config.ConfigException:
                log.warning(
                    "k8s client: no config available — event collection disabled"
                )
                self._enabled = False
                return
        self._enabled = True
        self._v1 = client.CoreV1Api()

    async def collect(
        self,
        namespace: str,
        window_minutes: int = 30,
        limit: int = 50,
    ) -> list[TimelineEvent]:
        """Return recent K8s events from ``namespace`` as TimelineEvents."""
        if not self._enabled:
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        try:
            # The kubernetes client is sync; in real production we'd push this
            # to a thread executor. For demo loads it's fine inline.
            resp = self._v1.list_namespaced_event(
                namespace=namespace,
                limit=limit,
                # Sort newest first via fieldSelector isn't supported here,
                # so we sort client-side after the fetch.
            )
        except ApiException as exc:
            log.warning("k8s events fetch failed for ns=%s: %s", namespace, exc.reason)
            return []

        events: list[TimelineEvent] = []
        for item in resp.items:
            ts = item.last_timestamp or item.event_time or item.first_timestamp
            if ts is None:
                continue
            ts_utc = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
            if ts_utc < cutoff:
                continue

            reason = item.reason or "Unknown"
            sev = HIGH_SIGNAL_REASONS.get(reason, "info")
            obj = item.involved_object
            who = f"{obj.kind}/{obj.name}" if obj else "unknown"
            count = item.count or 1
            count_str = f" (×{count})" if count > 1 else ""

            events.append(
                TimelineEvent(
                    ts=ts_utc,
                    source="k8s_event",
                    summary=f"{reason} on {who}{count_str}: {item.message or ''}".strip(),
                    severity=sev,
                    detail={
                        "reason": reason,
                        "object": who,
                        "count": str(count),
                        "type": item.type or "",
                    },
                )
            )

        events.sort(key=lambda e: e.ts)
        log.debug("collected %d events from ns=%s", len(events), namespace)
        return events
