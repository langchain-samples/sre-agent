"""Job Inspector subagent — analyzes Kubernetes Jobs and CronJobs."""
from config import SUBAGENT_MODEL
from tools import (
    kubectl_get_namespaces,
    kubectl_get_jobs,
    kubectl_get_cronjobs,
    kubectl_get_pod_logs,
    kubectl_get_events,
)

job_inspector_subagent = {
    "name": "job-inspector",
    "model": SUBAGENT_MODEL,
    "description": (
        "Inspect Kubernetes Jobs and CronJobs. Identifies failed jobs, suspended CronJobs, "
        "jobs stuck in active state, and CronJobs that haven't run successfully recently. "
        "Fetches logs and events for failed jobs to diagnose root causes."
    ),
    "system_prompt": (
        "You are a Kubernetes batch workload specialist. When asked to inspect jobs:\n"
        "1. Run kubectl_get_jobs with namespace='--all-namespaces'\n"
        "2. Run kubectl_get_cronjobs with namespace='--all-namespaces'\n"
        "3. For each failed or stuck job, fetch events and logs from its pods to diagnose the cause\n"
        "4. Return a structured report:\n"
        "   - CRITICAL: jobs that are failing repeatedly, CronJobs with no successful run in >2x their interval\n"
        "   - WARNING: suspended CronJobs (may be intentional — flag for review), "
        "jobs stuck in active state for >1h\n"
        "   - INFO: recently completed job history, upcoming schedule times\n"
        "   - ROOT CAUSES: for each failing job, explain what the logs/events show\n"
        "   - RECOMMENDATIONS: restart failed jobs, unsuspend if appropriate, fix job image/config\n"
        "Be specific — include job names, failure counts, and error messages from logs."
    ),
    "tools": [
        kubectl_get_namespaces,
        kubectl_get_jobs,
        kubectl_get_cronjobs,
        kubectl_get_pod_logs,
        kubectl_get_events,
    ],
}
