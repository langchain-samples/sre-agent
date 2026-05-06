"""Log Analyzer subagent — detects errors and anomalies in pod logs."""
from config import SUBAGENT_MODEL
from tools import (
    kubectl_get_pods,
    kubectl_get_pod_logs,
    kubectl_get_events,
    kubectl_get_namespaces,
    kubectl_describe_pod,
)

log_analyzer_subagent = {
    "name": "log-analyzer",
    "model": SUBAGENT_MODEL,
    "description": (
        "Analyze pod logs across namespaces to detect errors, exceptions, panics, "
        "OOM kills, connection failures, and other anomalies. Identifies error "
        "patterns and surfaces the most critical issues with context."
    ),
    "system_prompt": (
        "You are a Kubernetes log analysis specialist focused on error detection.\n"
        "When asked to analyze logs:\n"
        "1. List all pods in the target namespace(s)\n"
        "2. For each pod, fetch recent logs (tail=200) and look for:\n"
        "   - ERROR, FATAL, PANIC, CRITICAL log lines\n"
        "   - Stack traces and exceptions\n"
        "   - Connection refused / timeout messages\n"
        "   - Out of memory signals\n"
        "   - Authentication/authorization failures\n"
        "   - Repeated errors (same error appearing multiple times)\n"
        "3. For crash-looping pods, also fetch --previous logs\n"
        "4. Check Warning events for context\n"
        "5. Group errors by type and frequency\n"
        "6. Return a structured report:\n"
        "   - CRITICAL ERRORS: errors causing crashes or service failures\n"
        "   - WARNINGS: recurring non-fatal issues\n"
        "   - ERROR PATTERNS: error type → affected pods → frequency\n"
        "   - LOG EXCERPTS: relevant log snippets (max 5 lines each)\n"
        "   - RECOMMENDATIONS: what to investigate or fix\n"
        "Be concise — surface signal, not noise. Skip INFO/DEBUG logs unless relevant."
    ),
    "tools": [
        kubectl_get_namespaces,
        kubectl_get_pods,
        kubectl_get_pod_logs,
        kubectl_get_events,
        kubectl_describe_pod,
    ],
}
