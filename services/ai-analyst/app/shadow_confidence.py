"""
Shadow-confidence check for high-severity verdicts.

WHY THIS EXISTS
---------------
The primary analyzer runs on Vertex AI Gemini, which does not expose
per-token logprobs. That means we can't measure how confident Gemini
actually was in the words it emitted — the ``confidence`` field in the
JSON schema is itself just another sampled token, not a calibrated
posterior.

For P1 incidents, "how much should we trust this verdict" is worth
knowing before we auto-remediate or wake up a human. This module runs
the *same alert + timeline + runbook evidence* through OpenAI's
gpt-4o-mini with ``logprobs=True``, computes the geometric-mean
per-token probability of the shadow response, and returns a
``ShadowResult`` that gets attached to the incident record.

The shadow is a SIGNAL, not a decision:
    high    (mean p >= 0.7)  → verdict is defensible
    medium  (0.4-0.7)         → borderline; annotate but don't gate
    low     (< 0.4)           → escalate: shadow was hedging heavily

DESIGN NOTES
------------
- Opt-in via ``SHADOW_CONFIDENCE_ENABLED=true`` and only fires for
  ``severity in ("critical", "P1")`` alerts. A cluster with 100 P3
  alerts an hour costs nothing extra; a real P1 fires the shadow.
- Runs asynchronously with respect to the primary fan-out. Slack and
  the dashboard get the Gemini verdict immediately. When the shadow
  completes (typically 1-3s later), it's attached to the Firestore
  incident record via ``IncidentStore.attach_shadow``.
- Graceful failure: if the OpenAI key is missing or the call errors,
  we log a warning and skip. The primary pipeline continues.
- Cost: ~$0.0002 per P1 alert at gpt-4o-mini rates. Free at
  portfolio-scale traffic; still cheap at real scale.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from datetime import datetime, timezone
from typing import Optional

from app.models import (
    Alert,
    AnalysisResult,
    IncidentTimeline,
    RunbookHit,
    ShadowResult,
)

log = logging.getLogger("ai-analyst.shadow")

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_MAX_TOKENS = 400
TRUST_HIGH_THRESHOLD = 0.7
TRUST_LOW_THRESHOLD = 0.4
LOW_CONF_TOKEN_P = 0.3


def _is_enabled() -> bool:
    return os.getenv("SHADOW_CONFIDENCE_ENABLED", "").lower() in ("1", "true", "yes")


def should_shadow(alert: Alert) -> bool:
    """Only shadow the alerts where a trust score is worth paying for."""
    if not _is_enabled():
        return False
    sev = (alert.labels.get("severity") or "").lower()
    return sev in ("critical", "p1")


class ShadowConfidenceChecker:
    """Wraps a logprob-enabled model call for the sanity-check flow."""

    def __init__(self, model: str = DEFAULT_MODEL, max_tokens: int = DEFAULT_MAX_TOKENS):
        self._model = model
        self._max_tokens = max_tokens
        self._client = None
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            log.warning("SHADOW_CONFIDENCE_ENABLED but OPENAI_API_KEY unset; shadow disabled")
            return
        try:
            from openai import AsyncOpenAI  # lazy import — optional dep
            self._client = AsyncOpenAI(api_key=api_key)
            log.info("Shadow confidence checker initialised model=%s", self._model)
        except ImportError:
            log.warning("openai package not installed; shadow disabled")

    async def check(
        self,
        alert: Alert,
        timeline: IncidentTimeline,
        runbook_context: list[RunbookHit],
        primary: AnalysisResult,
    ) -> Optional[ShadowResult]:
        """Fetch a shadow analysis; return a trust signal.

        On any error we log and return None — the primary verdict still ships.
        """
        if self._client is None:
            return None

        prompt = self._build_prompt(alert, timeline, runbook_context, primary)
        try:
            resp = await self._client.chat.completions.create(
                model=self._model,
                max_tokens=self._max_tokens,
                temperature=0.2,           # match the primary analyzer's temperature
                logprobs=True,
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": prompt},
                ],
            )
        except Exception as exc:  # noqa: BLE001 — third-party API surface is wide
            log.warning("shadow call failed: %s", exc)
            return None

        choice = resp.choices[0]
        content = choice.message.content or ""
        lp = choice.logprobs
        if not lp or not lp.content:
            log.warning("shadow response lacked logprobs; skipping")
            return None

        n = len(lp.content)
        total_logp = sum(t.logprob for t in lp.content)
        mean_p = math.exp(total_logp / n) if n else 0.0
        n_low = sum(1 for t in lp.content if math.exp(t.logprob) < LOW_CONF_TOKEN_P)

        signal: str
        if mean_p >= TRUST_HIGH_THRESHOLD:
            signal = "high"
        elif mean_p >= TRUST_LOW_THRESHOLD:
            signal = "medium"
        else:
            signal = "low"

        log.info(
            "shadow incident=%s mean_p=%.3f n_tokens=%d n_low=%d signal=%s",
            primary.incident_id, mean_p, n, n_low, signal,
        )
        return ShadowResult(
            model=self._model,
            geometric_mean_p=round(mean_p, 4),
            n_tokens=n,
            n_low_conf_tokens=n_low,
            trust_signal=signal,  # type: ignore[arg-type]
            summary_text=content[:1000],
            generated_at=datetime.now(timezone.utc),
        )

    @staticmethod
    def _build_prompt(
        alert: Alert,
        timeline: IncidentTimeline,
        runbook_context: list[RunbookHit],
        primary: AnalysisResult,
    ) -> str:
        """Same evidence Gemini saw, framed as a request for a plain paragraph.

        We deliberately DO NOT show the shadow model Gemini's verdict — we
        want an independent read. If we primed it with the primary answer
        it would just parrot high-confidence agreement.
        """
        lines: list[str] = []
        lines.append("## Firing alert")
        lines.append(f"- alertname:   {alert.labels.get('alertname','?')}")
        lines.append(f"- namespace:   {alert.labels.get('namespace','?')}")
        lines.append(f"- severity:    {alert.labels.get('severity','?')}")
        lines.append(f"- summary:     {alert.annotations.get('summary','')}")
        lines.append(f"- description: {alert.annotations.get('description','')}")

        candidates = timeline.causal_candidates() if timeline else []
        lines.append("\n## Causal candidates (pre-alert timeline)")
        if not candidates:
            lines.append("(none)")
        else:
            for e in sorted(candidates, key=lambda e: e.correlation_score, reverse=True)[:10]:
                lines.append(f"- [{e.ts.isoformat()}] [{e.source}] {e.summary}")

        if runbook_context:
            lines.append("\n## Runbook excerpts")
            for rb in runbook_context[:3]:
                lines.append(f"### {rb.title}")
                lines.append(rb.excerpt[:600])

        lines.append(
            "\nIn 2-3 sentences of plain prose, state the most probable "
            "root cause of this alert given the evidence above. Cite "
            "specific events. Do NOT hedge with 'possibly' if the evidence "
            "supports the claim; do NOT overclaim if it doesn't."
        )
        return "\n".join(lines)


_SYSTEM = (
    "You are an SRE reviewing an incident. Answer only what the evidence "
    "supports. Concise, specific, no filler. Plain prose — no JSON, no lists."
)


async def maybe_run_shadow(
    checker: Optional[ShadowConfidenceChecker],
    alert: Alert,
    timeline: IncidentTimeline,
    runbook_context: list[RunbookHit],
    primary: AnalysisResult,
    attach: "callable",
) -> None:
    """Run shadow, then invoke ``attach(incident_id, shadow_result)``.

    Designed to be launched as a fire-and-forget task so the primary
    fan-out isn't blocked by the shadow's latency.
    """
    if checker is None or not should_shadow(alert):
        return
    result = await checker.check(alert, timeline, runbook_context, primary)
    if result is None:
        return
    try:
        await attach(primary.incident_id, result)
    except Exception as exc:  # noqa: BLE001
        log.warning("failed to attach shadow to incident %s: %s", primary.incident_id, exc)
