"""Slack notification client — Block Kit formatted messages and HITL buttons."""
from __future__ import annotations
import logging
import os
import re
from typing import Optional

from config import MONITOR_ACK_HOURS
from monitor_state import humanize_age

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
        # chat.update requires a channel ID, not a name like "#sre_alerts".
        # Cached from the first successful post to self.channel (Slack returns the
        # resolved ID in resp["channel"]) so HITL button updates don't 404.
        self._channel_id = None
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

    def send_structured_report(
        self,
        report,
        source: str = "scheduled",
        channel: Optional[str] = None,
        thread_ts: Optional[str] = None,
        diff=None,
        report_id: Optional[str] = None,
    ) -> Optional[str]:
        """Render typed findings and optional history into Slack, marking incomplete analysis."""
        if not self.enabled:
            log.info(
                "[SLACK DISABLED] Structured report (severity=%s, findings=%d)",
                report.overall_severity, len(report.findings),
            )
            return None

        if not report.analysis_complete:
            diff = None

        # With a diff, the headline reflects what is *currently active and not
        # acked* rather than the raw report — an all-acked report is not "critical".
        if diff is not None:
            active_sevs = {(d.finding.severity or "").lower() for d in diff.active}
            has_critical = "critical" in active_sevs
            has_issues = bool(active_sevs & {"critical", "warning", "info"})
            recovered = not diff.active and bool(diff.resolved)
        else:
            has_issues = report.has_issues
            has_critical = report.overall_severity == "critical"
            recovered = False

        emoji = (
            ":red_circle:" if has_critical
            else ":large_yellow_circle:" if has_issues
            else ":large_green_circle:"
        )
        title = "Cluster Health Report" + (
            " — Critical Issues Found" if has_critical
            else " — Issues Found" if has_issues
            else " — Recovered" if recovered
            else " — All Clear"
        )
        if not report.analysis_complete:
            title = "Cluster Health Report — Analysis Incomplete"

        def _trunc(s: str, n: int) -> str:
            return s[:n] + "\n_(truncated)_" if len(s) > n else s

        # Header + summary go in top-level blocks. When top-level `blocks` are set,
        # Slack uses `text` only as the notification fallback (not rendered in-body),
        # which avoids the title showing twice (once as text, once as the header).
        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": f"{emoji}  {title}"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": _trunc(report.summary, 2700)}},
        ]

        attachments = []

        def _status_prefix(delta) -> str:
            """Lead each finding with what changed about it since the last check."""
            if delta.status == "new":
                repeat = (
                    f" · returned, {delta.times_seen}× total"
                    if delta.times_seen > 1 else ""
                )
                return f":new: *NEW*{repeat} — "
            if delta.status == "escalated":
                prev = (delta.previous_severity or "?").lower()
                return f":arrow_upper_right: *ESCALATED* {prev}→{delta.finding.severity} — "
            return f"_ongoing {humanize_age(delta.age)} · seen {delta.times_seen}×_ — "

        # With a diff we render the deltas (so each line carries its history);
        # without one we fall back to the plain findings list.
        grouped: dict[str, list] = {}
        if diff is not None:
            for delta in diff.active:
                grouped.setdefault((delta.finding.severity or "").lower(), []).append(delta)
        else:
            for f in report.findings:
                grouped.setdefault((f.severity or "").lower(), []).append(f)

        # One attachment per severity group, in priority order.
        for sev in ("critical", "warning", "info"):
            group = grouped.get(sev) or []
            if not group:
                continue
            sev_emoji = SEVERITY_EMOJI.get(sev, ":white_circle:")
            color = SEVERITY_COLOR.get(sev, "#718096")
            lines = []
            for item in group:
                f = item.finding if diff is not None else item
                ns = f" · `{f.namespace}`" if f.namespace else ""
                prefix = _status_prefix(item) if diff is not None else ""
                lines.append(f"• {prefix}*{f.title}*{ns} — {f.detail}")
            attachments.append({
                "color": color,
                "blocks": [{
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": _trunc(f"{sev_emoji} *{sev.upper()}*\n" + "\n".join(lines), 2700),
                    },
                }],
            })

        if diff is not None and diff.resolved:
            lines = [
                f"• *{r.title}*" + (f" · `{r.namespace}`" if r.namespace else "")
                for r in diff.resolved
            ]
            attachments.append({
                "color": "#38a169",
                "blocks": [{
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": _trunc(
                            ":white_check_mark: *RESOLVED SINCE LAST CHECK*\n" + "\n".join(lines),
                            2700,
                        ),
                    },
                }],
            })

        if report.recommended_actions:
            rec = "\n".join(f"{i}. {a}" for i, a in enumerate(report.recommended_actions, 1))
            attachments.append({
                "color": "#4a5568",
                "blocks": [{
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f":clipboard: *Recommended Actions*\n{_trunc(rec, 1200)}"},
                }],
            })

        # Ack mutes every finding in this report for a window, so a known issue
        # someone is already working on stops interrupting the channel.
        if report_id and diff is not None and diff.active:
            attachments.append({
                "color": "#4a5568",
                "blocks": [{
                    "type": "actions",
                    "elements": [{
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": f"Ack these for {MONITOR_ACK_HOURS}h",
                        },
                        "action_id": "sre_ack",
                        "value": report_id,
                    }],
                }],
            })

        context_bits = [f"Source: {source}"]
        if diff is not None:
            context_bits.append(diff.summary_line())
            if diff.suppressed:
                context_bits.append(f"{len(diff.suppressed)} acked (hidden)")
        attachments.append({
            "color": "#2d3748",
            "blocks": [{
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": " · ".join(context_bits)}],
            }],
        })

        post_kwargs = {
            "channel": channel or self.channel,
            "text": f"{emoji} {title}",
            "blocks": blocks,
            "attachments": attachments,
        }
        if thread_ts:
            post_kwargs["thread_ts"] = thread_ts
        try:
            resp = self._client.chat_postMessage(**post_kwargs)
            if not channel:  # posted to the default channel — cache its resolved ID
                self._channel_id = resp.get("channel") or self._channel_id
            return resp["ts"]
        except Exception as e:
            log.error("Slack post failed: %s", e)
            return None

    def send_health_report(
        self,
        summary: str,
        has_issues: bool = False,
        source: str = "scheduled",
    ) -> Optional[str]:
        """Send a periodic health report from free text.

        Legacy path: regex-parses `[CRITICAL]`/`[WARNING]`/`[INFO]` section markers
        out of an agent-produced markdown string. Prefer send_structured_report,
        which renders from a typed HealthReport. Still used for the main agent's
        final text summary (api.py)."""
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

    def mark_hitl_processing(self, message_ts: str, actor: str, action: str):
        """Swap the Approve/Reject buttons for an in-flight 'processing' state.

        Called immediately on button click so the user can't double-click and gets
        instant visual feedback. update_hitl_resolved is called later with the
        final outcome.
        """
        if not self.enabled or not message_ts:
            return
        try:
            # The buttons live in an attachment (see _post). chat.update leaves
            # omitted fields intact, so we must REPLACE `attachments` (not just set
            # top-level blocks) to actually remove the buttons.
            self._client.chat_update(
                channel=self._channel_id or self.channel,
                ts=message_ts,
                text=f":hourglass_flowing_sand: {action} processing",
                blocks=[],
                attachments=[{
                    "color": "#d69e2e",
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f":hourglass_flowing_sand: *{action} from* `{actor}` *received — processing…*",
                            },
                        },
                    ],
                }],
            )
        except Exception as e:
            log.warning("Failed to mark HITL processing: %s", e)

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
            # Replace the attachment (which holds the buttons) so they're removed;
            # setting only top-level blocks would leave the buttoned attachment intact.
            self._client.chat_update(
                channel=self._channel_id or self.channel,
                ts=message_ts,
                text=f"{emoji} Change {verdict}",
                blocks=[],
                attachments=[{
                    "color": "#2f855a" if approved else "#c53030",
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"{emoji} *Change {verdict}*{by_text}",
                            },
                        },
                        *result_section,
                    ],
                }],
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
            self._channel_id = resp.get("channel") or self._channel_id
            return resp["ts"]
        except Exception as e:
            log.error("Slack post failed: %s", e)
            return None


def make_notifier() -> SlackNotifier:
    return SlackNotifier(
        bot_token=os.getenv("SLACK_BOT_TOKEN", ""),
        channel=os.getenv("SLACK_CHANNEL", "#sre-alerts"),
    )
