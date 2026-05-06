"""Pod Inspector subagent — analyzes pod health, restarts, and failures."""
from config import SUBAGENT_MODEL
from tools import (
    kubectl_get_pods,
    kubectl_describe_pod,
    kubectl_get_pod_logs,
    kubectl_get_events,
    kubectl_get_namespaces,
)

pod_inspector_subagent = {
    "name": "pod-inspector",
    "model": SUBAGENT_MODEL,
    "description": (
        "Inspect pod health across namespaces. Identifies CrashLoopBackOff, "
        "OOMKilled, Pending, Evicted, and ImagePullBackOff issues. Fetches logs "
        "and events to diagnose root causes. Returns a structured health report "
        "with findings and recommended actions."
    ),
    "system_prompt": (
        "You are a Kubernetes pod health specialist. When asked to inspect pods:\n"
        "1. List all pods in the specified namespace(s)\n"
        "2. Identify any pods NOT in Running/Completed state\n"
        "3. For each unhealthy pod: describe it and fetch recent logs (including --previous for crash loops)\n"
        "4. Check Warning events related to unhealthy pods\n"
        "5. Diagnose the root cause (OOM, crash, image pull error, scheduling failure, etc.)\n"
        "6. Return a structured report:\n"
        "   - HEALTHY: count of healthy pods\n"
        "   - ISSUES: list each problem pod with diagnosis\n"
        "   - ROOT CAUSES: concise explanation per issue\n"
        "   - RECOMMENDATIONS: specific actions to fix each issue\n"
        "Be specific — include pod names, namespaces, error messages from logs."
    ),
    "tools": [
        kubectl_get_namespaces,
        kubectl_get_pods,
        kubectl_describe_pod,
        kubectl_get_pod_logs,
        kubectl_get_events,
    ],
}
