"""Fan-out of completed verdicts to Slack and the dashboard."""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from app.models import AnalysisResult

log = logging.getLogger("ai-analyst.notifier")


class Notifier:
    def __init__(
        self,
        slack_webhook: Optional[str] = None,
        dashboard_url: Optional[str] = None,
    ):
        self._slack = slack_webhook
        self._dashboard = dashboard_url
        self._client = httpx.AsyncClient(timeout=10)

    async def fanout(self, result: AnalysisResult) -> None:
        if self._slack:
            try:
                await self._client.post(self._slack, json=self._slack_blocks(result))
            except httpx.HTTPError:
                log.exception("Slack post failed")
        if self._dashboard:
            try:
                await self._client.post(
                    f"{self._dashboard.rstrip('/')}/incidents",
                    json=result.model_dump(mode="json"),
                )
            except httpx.HTTPError:
                log.exception("Dashboard ingest failed")

    @staticmethod
    def _slack_blocks(r: AnalysisResult) -> dict:
        confidence_emoji = (
            ":large_green_circle:" if r.confidence >= 0.8
            else ":large_yellow_circle:" if r.confidence >= 0.5
            else ":red_circle:"
        )
        # Top 3 evidence rows are usually enough for a Slack post; the
        # dashboard incident detail view shows the full list.
        evidence_lines = "\n".join(
            f"• [{e.source}] {e.observation}" for e in r.evidence[:3]
        ) or "_no evidence cited_"
        steps = "\n".join(
            f"{i+1}. {s}" for i, s in enumerate(r.remediation_steps[:5])
        )
        runbooks = (
            ", ".join(rb.title for rb in r.runbook_refs) or "_no runbook hits_"
        )
        timeline_summary = (
            f"{len(r.timeline.events)} timeline events"
            if r.timeline else "no timeline"
        )

        text = (
            f"*AI SRE Analyst* — `{r.namespace}` `{r.severity}`\n"
            f"{confidence_emoji} *Confidence:* {r.confidence:.0%}  "
            f"*Tier:* {r.tier}  ·  {timeline_summary}\n\n"
            f"*Probable cause:* {r.probable_cause}\n"
            f"*Recommended action:* {r.recommended_action}\n\n"
            f"*Evidence:*\n{evidence_lines}\n\n"
            f"*Blast radius:* {', '.join(r.blast_radius) or '_unknown_'}\n"
            f"*Runbooks:* {runbooks}\n\n"
            f"*Remediation steps:*\n{steps}"
        )
        return {
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": text}},
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "✅ Correct"},
                            "value": f"correct:{r.incident_id}",
                            "action_id": "feedback_correct",
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "✏️ Correct it"},
                            "value": f"wrong:{r.incident_id}",
                            "action_id": "feedback_wrong",
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "🔍 Open timeline"},
                            "url": f"https://dash.example.com/incidents/{r.incident_id}",
                            "action_id": "open_timeline",
                        },
                    ],
                },
            ]
        }
