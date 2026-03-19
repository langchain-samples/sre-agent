"""Slack notification tool — lets the agent proactively send alerts and reports."""
from __future__ import annotations
from langchain.tools import tool


def make_slack_notification_tool(notifier):
    """
    Factory: bind a SlackNotifier instance to a LangChain tool.
    Call this once at startup after creating the notifier.
    """

    @tool
    def send_slack_notification(
        severity: str,
        title: str,
        message: str,
        namespace: str = "",
    ) -> str:
        """
        Send a notification to the Slack alerts channel.

        severity: 'critical', 'warning', 'info', or 'ok'
        title: short headline (e.g. 'CrashLoopBackOff detected')
        message: detailed finding or recommendation
        namespace: Kubernetes namespace (optional context)

        Use this tool to report:
        - Critical issues that need immediate attention
        - Findings from health checks
        - Successful changes that were applied
        - Completion summaries after an audit
        """
        ts = notifier.send_alert(severity, title, message, namespace)
        status = "sent" if ts else "skipped (Slack not configured)"
        return f"Slack notification {status}: [{severity.upper()}] {title}"

    return send_slack_notification
