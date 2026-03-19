"""Slack notification client — Block Kit formatted messages and HITL buttons."""
from __future__ import annotations
import logging
import os
from typing import Optional

log = logging.getLogger("sre-bot.slack")

SEVERITY_EMOJI = {
    "critical": ":red_circle:",
    "warning": ":large_yellow_circle:",
    "info": ":large_blue_circle:",
    "ok": ":large_green_circle:",
}

SEVERITY_COLOR = {
    "critical": "#e53e3e",
    "warning": "#d69e2e",
    "info": "#3182ce",
    "ok": "#38a169",
}


class SlackNotifier:
    def __init__(self, bot_token: str, channel: str):
        self.channel = channel
        self._client = None
        if bot_token:
            try:
                from slack_sdk import WebClient
                self._client = WebClient(token=bot_token)
                log.info("Slack notifier initialized (channel=%s)", channel)
            except Exception as e:
                log.warning("Failed to init Slack client: %s", e)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send_alert(
        self,
        severity: str,
        title: str,
        message: str,
        namespace: str = "",
    ) -> Optional[str]:
        """Send a severity-tagged alert. Returns message ts or None."""
        if not self.enabled:
            log.info("[SLACK DISABLED] %s | %s: %s", severity.upper(), title, message[:120])
            return None

        emoji = SEVERITY_EMOJI.get(severity.lower(), ":white_circle:")
        color = SEVERITY_COLOR.get(severity.lower(), "#718096")
        ns_text = f" · `{namespace}`" if namespace else ""

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{emoji} *{title}*{ns_text}\n{message}",
                },
            }
        ]
        return self._post(blocks=blocks, color=color, text=f"{emoji} {title}")

    def send_health_report(
        self,
        summary: str,
        has_issues: bool = False,
        source: str = "scheduled",
    ) -> Optional[str]:
        """Send a periodic health report — green if clean, yellow/red if issues found."""
        if not self.enabled:
            log.info("[SLACK DISABLED] Health report (has_issues=%s)", has_issues)
            return None

        emoji = ":large_yellow_circle:" if has_issues else ":large_green_circle:"
        color = "#d69e2e" if has_issues else "#38a169"
        title = "Cluster Health Report" + (" — Issues Found" if has_issues else " — All Clear")

        # Truncate summary for Slack (max 3000 chars per block)
        truncated = summary[:2800] + "\n_(truncated)_" if len(summary) > 2800 else summary

        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{emoji}  {title}"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": truncated},
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"Source: {source}"}],
            },
        ]
        return self._post(blocks=blocks, color=color, text=title)

    def send_hitl_request(
        self,
        session_id: str,
        action_description: str,
    ) -> Optional[str]:
        """
        Post an approval request with Approve / Reject buttons.
        Returns the message ts so it can be updated after the decision.
        """
        if not self.enabled:
            log.info("[SLACK DISABLED] HITL request for session %s: %s", session_id, action_description[:100])
            return None

        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": ":warning:  SRE Bot — Approval Required"},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"The SRE bot wants to apply a change:\n```{action_description[:1200]}```",
                },
            },
            {
                "type": "actions",
                "block_id": f"hitl_{session_id}",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✓  Approve"},
                        "style": "primary",
                        "action_id": "sre_approve",
                        "value": session_id,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✗  Reject"},
                        "style": "danger",
                        "action_id": "sre_reject",
                        "value": session_id,
                    },
                ],
            },
        ]
        return self._post(blocks=blocks, color="#d69e2e", text=":warning: Approval Required")

    def update_hitl_resolved(
        self,
        message_ts: str,
        approved: bool,
        actor: str = "",
        result: str = "",
    ):
        """Replace the HITL message with the outcome (removes buttons)."""
        if not self.enabled or not message_ts:
            return
        try:
            emoji = ":white_check_mark:" if approved else ":no_entry_sign:"
            verdict = "Approved" if approved else "Rejected"
            by_text = f" by *{actor}*" if actor else ""
            result_section = (
                [{"type": "section", "text": {"type": "mrkdwn", "text": f"Result: {result[:500]}"}}]
                if result else []
            )
            self._client.chat_update(
                channel=self.channel,
                ts=message_ts,
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"{emoji} *Change {verdict}*{by_text}",
                        },
                    },
                    *result_section,
                ],
                text=f"{emoji} Change {verdict}",
            )
        except Exception as e:
            log.warning("Failed to update HITL message: %s", e)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _post(
        self,
        blocks: list,
        color: str = "#718096",
        text: str = "SRE Bot notification",
    ) -> Optional[str]:
        """Post a message and return the ts."""
        try:
            resp = self._client.chat_postMessage(
                channel=self.channel,
                text=text,
                attachments=[{"color": color, "blocks": blocks}],
            )
            return resp["ts"]
        except Exception as e:
            log.error("Slack post failed: %s", e)
            return None


def make_notifier() -> SlackNotifier:
    return SlackNotifier(
        bot_token=os.getenv("SLACK_BOT_TOKEN", ""),
        channel=os.getenv("SLACK_CHANNEL", "#sre-alerts"),
    )
