"""
Cost guard: severity-tiered LLM dispatch + daily token budget.

The naive v1 design called Gemini for every alert that escaped tier-1
rules. That's expensive and rarely necessary — most P3 alerts are
fine with a deterministic summary built from the timeline alone, and
P2 alerts often don't need RAG.

Policy (configurable via env / Helm values):

    P3 (info/notice)   → no LLM call. Format the timeline + tier-1
                         partial matches into a deterministic summary.
    P2 (warning)       → small LLM call: Gemini Flash, no RAG, short
                         prompt. Cheap.
    P1 (critical)      → full pipeline: RAG top-k=4, Gemini Flash with
                         the entire timeline, structured output.

In addition we enforce a global daily token budget. When we cross
the soft-limit (default 80%), we degrade P2 alerts to "no LLM" until
the next UTC day rolls over. When we cross the hard-limit (100%),
we degrade P1 alerts too — better to ship a deterministic summary
than to throw 503s.

Budget state lives in Redis under a key keyed by UTC date so it
naturally rolls over.

A model fallback is supported: if the primary model call raises,
we retry once against `fallback_model` (default: same model — the
fallback hook is plumbed in case the operator wants Flash → Lite).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Optional

import redis.asyncio as redis

log = logging.getLogger("ai-analyst.cost_guard")

DispatchTier = Literal["none", "summary_only", "llm_no_rag", "full_rag_llm"]


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


@dataclass
class DispatchDecision:
    tier: DispatchTier
    use_llm: bool
    use_rag: bool
    reason: str


class CostGuard:
    """Decides how much LLM budget to spend on each alert."""

    def __init__(
        self,
        redis_url: str,
        daily_token_budget: int = 1_000_000,
        soft_limit_ratio: float = 0.80,
    ):
        self._url = redis_url
        self._client: redis.Redis | None = None
        self._budget = daily_token_budget
        self._soft = soft_limit_ratio
        self._cache: dict[str, tuple[float, str]] = {}  # in-process verdict cache
        self._cache_ttl = 60.0  # seconds

    async def connect(self) -> None:
        # Reuse the same Redis used by dedup; the keys don't collide.
        self._client = redis.from_url(self._url, decode_responses=True)
        await self._client.ping()

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    # ----- Severity → dispatch decision ------------------------------------
    async def decide(self, severity_label: str) -> DispatchDecision:
        sev = self._normalize_severity(severity_label)
        used = await self._tokens_used_today()
        budget_ratio = used / max(self._budget, 1)

        # Hard limit: cap everything to deterministic summaries.
        if budget_ratio >= 1.0:
            return DispatchDecision(
                tier="summary_only",
                use_llm=False,
                use_rag=False,
                reason=(
                    f"daily token budget exhausted ({used}/{self._budget}); "
                    "degrading to deterministic summaries"
                ),
            )

        # Soft limit: drop P2 to no-LLM, keep P1 full.
        soft_breached = budget_ratio >= self._soft

        if sev == "P3":
            return DispatchDecision(
                tier="summary_only",
                use_llm=False,
                use_rag=False,
                reason="P3 alerts use deterministic summary (policy)",
            )

        if sev == "P2":
            if soft_breached:
                return DispatchDecision(
                    tier="summary_only",
                    use_llm=False,
                    use_rag=False,
                    reason=(
                        f"soft budget breached ({budget_ratio:.0%}); "
                        "degrading P2 to deterministic summary"
                    ),
                )
            return DispatchDecision(
                tier="llm_no_rag",
                use_llm=True,
                use_rag=False,
                reason="P2 alerts use Gemini without RAG (policy)",
            )

        # P1 / critical
        return DispatchDecision(
            tier="full_rag_llm",
            use_llm=True,
            use_rag=True,
            reason="P1/critical alert uses full RAG + Gemini",
        )

    # ----- Token accounting -----------------------------------------------
    async def record_tokens(self, in_tokens: int, out_tokens: int) -> None:
        if not self._client:
            return
        key = f"aianalyst:tokens:{_today_utc()}"
        # 36h TTL so the next-day key naturally takes over.
        async with self._client.pipeline() as pipe:
            pipe.incrby(key, in_tokens + out_tokens)
            pipe.expire(key, 60 * 60 * 36)
            await pipe.execute()

    async def _tokens_used_today(self) -> int:
        if not self._client:
            return 0
        key = f"aianalyst:tokens:{_today_utc()}"
        v = await self._client.get(key)
        try:
            return int(v) if v else 0
        except (TypeError, ValueError):
            return 0

    # ----- Verdict cache --------------------------------------------------
    def cached_verdict(self, fingerprint: str) -> Optional[str]:
        """Return a cached verdict_json if we've analyzed this fp recently."""
        entry = self._cache.get(fingerprint)
        if not entry:
            return None
        ts, verdict_json = entry
        if (time.monotonic() - ts) > self._cache_ttl:
            self._cache.pop(fingerprint, None)
            return None
        return verdict_json

    def cache_verdict(self, fingerprint: str, verdict_json: str) -> None:
        # Bound the cache so memory stays sane.
        if len(self._cache) > 256:
            oldest = min(self._cache.items(), key=lambda kv: kv[1][0])[0]
            self._cache.pop(oldest, None)
        self._cache[fingerprint] = (time.monotonic(), verdict_json)

    # ----- Severity normalisation -----------------------------------------
    @staticmethod
    def _normalize_severity(label: str) -> str:
        l = (label or "").lower()
        if l in ("critical", "p1", "sev1"):
            return "P1"
        if l in ("warning", "p2", "sev2"):
            return "P2"
        if l in ("info", "notice", "p3", "sev3"):
            return "P3"
        # Unknown: treat as P2, the safe middle ground.
        return "P2"


# --- Helpers used when degrading to deterministic summaries --------------
def fallback_summary_from_timeline(
    alert_summary: str,
    timeline_events: list,
) -> tuple[str, str, list[str]]:
    """Build a deterministic verdict when we don't call the LLM.

    Returns (probable_cause, recommended_action, remediation_steps)
    in the same shape the analyzer produces. Used by main.py when
    the cost guard says "no LLM".
    """
    has_deploy = any(getattr(e, "source", None) == "deployment" for e in timeline_events)
    has_oom = any(
        getattr(e, "source", None) == "k8s_event"
        and "oom" in (getattr(e, "summary", "") or "").lower()
        for e in timeline_events
    )

    if has_oom:
        cause = "OOMKill events present in the timeline; container hit memory limit."
        action = "Bump container memory limit by 25% and roll the deployment."
        steps = [
            "Capture heap snapshot if possible before pod restart",
            "Bump memory limit by 25% via Helm values",
            "kubectl rollout restart on the affected deployment",
        ]
    elif has_deploy:
        cause = (
            f"Alert '{alert_summary}' fired and a recent deployment is in the timeline; "
            "deployment is the most likely cause."
        )
        action = "Inspect the recent deployment; consider rollback if symptoms persist."
        steps = [
            "kubectl rollout history on the deployment in this namespace",
            "kubectl rollout undo if a deploy landed in the last 30 minutes",
            "If rollback resolves it, escalate to the owning team for forward fix",
        ]
    else:
        cause = (
            f"Alert '{alert_summary}' fired with no recent deployments or "
            "obvious K8s events. Triage by hand."
        )
        action = "Page on-call SRE for manual triage; signal is ambiguous."
        steps = [
            "Inspect pod status and logs for the affected service",
            "Check upstream dependencies (Vertex AI, Qdrant, Redis) for outage",
        ]
    return cause, action, steps
