"""Slack notification client — Block Kit formatted messages and HITL buttons."""
from __future__ import annotations
import logging
import os
import re
from typing import Optional

log = logging.getLogger("sre-agent.slack")

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

        # Normalize markdown bold to Slack mrkdwn bold
        text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", summary)

        has_critical = bool(re.search(r"critical issues?|\[CRITICAL\]", summary, re.IGNORECASE))
        emoji = ":large_green_circle:" if not has_issues else (":red_circle:" if has_critical else ":large_yellow_circle:")
        title = "Cluster Health Report" + (" — Critical Issues Found" if has_critical else " — Issues Found" if has_issues else " — All Clear")

        _section_styles = {
            "critical": (":red_circle:", "#e53e3e"),
            "warning":  (":large_yellow_circle:", "#d69e2e"),
            "info":     (":large_blue_circle:", "#3182ce"),
        }

        # Normalise free-form section headers to canonical tokens before parsing
        text = re.sub(r"\*?(Critical Issues?|CRITICAL):?\*?", "[CRITICAL]", text, flags=re.IGNORECASE)
        text = re.sub(r"\*?(Warning Issues?|Secondary Issues?|WARNING):?\*?", "[WARNING]", text, flags=re.IGNORECASE)
        text = re.sub(r"\*?(Info(?:rmation)?|Optimizations?|INFO):?\*?", "[INFO]", text, flags=re.IGNORECASE)
        text = re.sub(r"\*?(Recommendations?|Recommended actions?):?\*?", "Recommended actions:", text, flags=re.IGNORECASE)

        # Split text into labelled sections + trailing recommendations
        section_re = re.compile(
            r"\[?(CRITICAL|WARNING|INFO)\]?[ \t]*\n?(.*?)(?=\n\[?(?:CRITICAL|WARNING|INFO)\]?|"
            r"\nRecommended actions:|\Z)",
            re.IGNORECASE | re.DOTALL,
        )
        rec_re = re.compile(r"Recommended actions:\s*(.*)", re.IGNORECASE | re.DOTALL)

        sections = list(section_re.finditer(text))
        rec_match = rec_re.search(text)

        def _trunc(s: str, n: int) -> str:
            return s[:n] + "\n_(truncated)_" if len(s) > n else s

        attachments = [
            {
                "color": "#38a169" if not has_issues else "#d69e2e",
                "blocks": [{"type": "header", "text": {"type": "plain_text", "text": f"{emoji}  {title}"}}],
            }
        ]

        if sections:
            for m in sections:
                sev = m.group(1).lower()
                content = m.group(2).strip()
                if not content:
                    continue
                sev_emoji, color = _section_styles.get(sev, (":white_circle:", "#718096"))
                attachments.append({
                    "color": color,
                    "blocks": [{
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f"{sev_emoji} *{sev.upper()}*\n{_trunc(content, 2700)}"},
                    }],
                })
        else:
            # No section markers — dump the full text (minus recommendations)
            body = text[:rec_match.start()].strip() if rec_match else text
            attachments.append({
                "color": "#d69e2e" if has_issues else "#718096",
                "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": _trunc(body, 2700)}}],
            })

        if rec_match:
            rec_body = rec_match.group(1).strip()
            if rec_body:
                attachments.append({
                    "color": "#4a5568",
                    "blocks": [{
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f":clipboard: *Recommended Actions*\n{_trunc(rec_body, 1200)}"},
                    }],
                })

        attachments.append({
            "color": "#2d3748",
            "blocks": [{"type": "context", "elements": [{"type": "mrkdwn", "text": f"Source: {source}"}]}],
        })

        try:
            resp = self._client.chat_postMessage(
                channel=self.channel,
                text=f"{emoji} {title}",
                attachments=attachments,
            )
            return resp["ts"]
        except Exception as e:
            log.error("Slack post failed: %s", e)
            return None

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
