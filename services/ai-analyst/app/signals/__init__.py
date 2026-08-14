"""Signal collectors — gather context beyond the firing alert.

Three sources, each with its own latency / cost profile:

- ``events.py``       — K8s Events API. Live calls, ~50ms, cheap.
- ``deployments.py``  — Deployment + ReplicaSet history. Live, ~80ms.
- ``logs.py``         — Cloud Logging. Async-cached, can be slow and
                       expensive, so we fetch in the background and
                       serve from a TTL cache.

All collectors return a list of ``TimelineEvent`` so the timeline
builder can merge them into one chronological view.
"""

from app.signals.deployments import DeploymentCollector
from app.signals.events import EventCollector
from app.signals.logs import LogCollector

__all__ = ["EventCollector", "DeploymentCollector", "LogCollector"]
