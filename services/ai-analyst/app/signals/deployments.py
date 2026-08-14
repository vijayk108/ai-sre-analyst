"""
Deployment / ReplicaSet history collector.

A recent rollout is the highest-prior root cause for almost every
non-trivial alert. "p95 climbed at 18:43, the deployment landed at
18:39" is a sentence that solves about 40% of incidents on its own.

We surface every Deployment whose ``creationTimestamp`` or last
``status.conditions[].lastUpdateTime`` falls inside the lookback
window — that catches both fresh rollouts and ongoing ones that just
finished progressing.

Live calls; latency target: <100ms.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from app.models import TimelineEvent

log = logging.getLogger("ai-analyst.signals.deployments")


class DeploymentCollector:
    def __init__(self):
        try:
            config.load_incluster_config()
        except config.ConfigException:
            try:
                config.load_kube_config()
            except config.ConfigException:
                self._enabled = False
                return
        self._enabled = True
        self._apps = client.AppsV1Api()

    async def collect(
        self, namespace: str, window_minutes: int = 60
    ) -> list[TimelineEvent]:
        if not self._enabled:
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        events: list[TimelineEvent] = []

        # --- Deployments: rollout events ----------------------------------
        try:
            deps = self._apps.list_namespaced_deployment(namespace=namespace)
        except ApiException as exc:
            log.warning("Deployment list failed for ns=%s: %s", namespace, exc.reason)
            return []

        for dep in deps.items:
            for cond in (dep.status.conditions or []):
                ts = cond.last_update_time or cond.last_transition_time
                if ts is None:
                    continue
                ts_utc = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
                if ts_utc < cutoff:
                    continue
                # Progressing=True with reason NewReplicaSetAvailable means a
                # rollout completed. Progressing=True with ReplicaSetUpdated
                # means one is in flight.
                if cond.type == "Progressing" and cond.reason in (
                    "NewReplicaSetAvailable",
                    "ReplicaSetUpdated",
                    "NewReplicaSetCreated",
                ):
                    sev = "info" if cond.reason == "NewReplicaSetAvailable" else "warning"
                    events.append(
                        TimelineEvent(
                            ts=ts_utc,
                            source="deployment",
                            summary=(
                                f"Deployment {dep.metadata.name}: "
                                f"{cond.reason} ({cond.message or ''})".strip()
                            ),
                            severity=sev,
                            detail={
                                "deployment": dep.metadata.name,
                                "reason": cond.reason,
                                "current_replicas": str(dep.status.replicas or 0),
                                "ready_replicas": str(dep.status.ready_replicas or 0),
                                "image": _first_image(dep),
                            },
                        )
                    )

        # --- ReplicaSets: image hash gives us the actual deploy timestamp -
        try:
            rs_list = self._apps.list_namespaced_replica_set(namespace=namespace)
        except ApiException as exc:
            log.warning("ReplicaSet list failed for ns=%s: %s", namespace, exc.reason)
            rs_list = None

        if rs_list is not None:
            for rs in rs_list.items:
                ts = rs.metadata.creation_timestamp
                if ts is None:
                    continue
                ts_utc = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
                if ts_utc < cutoff:
                    continue
                # Skip the ReplicaSet that's been at 0 desired forever — those
                # are old generations that didn't actually roll.
                if (rs.spec.replicas or 0) == 0 and (rs.status.replicas or 0) == 0:
                    continue
                events.append(
                    TimelineEvent(
                        ts=ts_utc,
                        source="deployment",
                        summary=(
                            f"ReplicaSet {rs.metadata.name} created "
                            f"(replicas: {rs.spec.replicas})"
                        ),
                        severity="info",
                        detail={
                            "replicaset": rs.metadata.name,
                            "owner": _owner_name(rs),
                            "image": _first_image_rs(rs),
                            "replicas": str(rs.spec.replicas or 0),
                        },
                    )
                )

        events.sort(key=lambda e: e.ts)
        log.debug("collected %d deployment events from ns=%s", len(events), namespace)
        return events


def _first_image(dep) -> str:
    try:
        return dep.spec.template.spec.containers[0].image
    except (AttributeError, IndexError):
        return ""


def _first_image_rs(rs) -> str:
    try:
        return rs.spec.template.spec.containers[0].image
    except (AttributeError, IndexError):
        return ""


def _owner_name(rs) -> str:
    refs = (rs.metadata.owner_references or [])
    return refs[0].name if refs else ""
