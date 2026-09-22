"""Resolution-memory lookup tool — lets the agent check for a known-working fix."""
from __future__ import annotations
from types import SimpleNamespace

from langchain.tools import tool

from monitor_state import fingerprint


def make_resolution_memory_tool(db):
    """
    Factory: bind a persistence handle to a LangChain tool.
    Call this once at startup, same pattern as make_slack_notification_tool.
    """

    @tool
    def check_resolution_memory(
        namespace: str,
        kind: str,
        resource_name: str,
        reason: str,
    ) -> str:
        """
        Check whether this exact finding pattern has a previously-approved fix
        on record, before proposing a new one.

        namespace: Kubernetes namespace (empty string for cluster-scoped resources)
        kind: resource kind, e.g. 'Deployment', 'Pod', 'HorizontalPodAutoscaler', 'Node'
        resource_name: the resource's name (raw pod names are fine — normalized internally)
        reason: the short failure reason, e.g. 'CrashLoopBackOff', 'OOMKilled'

        Use this before delegating a fix to change-executor. A result does not
        mean the fix will be auto-applied — HITL approval is still required
        every time — but it tells you (and the human approving it) whether this
        exact pattern has been solved before, and how many times it has worked.
        """
        fp = fingerprint(
            SimpleNamespace(namespace=namespace, kind=kind, resource_name=resource_name, reason=reason)
        )
        fixes = db.find_known_fixes(fp)
        if not fixes:
            return "No known fix on record for this exact finding."

        best = fixes[0]
        times = best.get("times_confirmed", 1)
        confidence = "confirmed" if times > 1 else "seen once, unconfirmed"
        when = best.get("last_confirmed_at")
        return (
            f"Known fix on record ({confidence}, worked {times}x, most recently "
            f"{when} by {best.get('last_actor') or 'unknown'}): "
            f"{best.get('tool_name')}({best.get('tool_args')})"
        )

    return check_resolution_memory
