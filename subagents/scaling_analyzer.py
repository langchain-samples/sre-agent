"""Scaling Analyzer subagent — evaluates HPA config, replica counts, and sizing."""
from tools import (
    kubectl_get_deployments,
    kubectl_describe_deployment,
    kubectl_get_hpa,
    kubectl_top_pods,
    kubectl_top_nodes,
    kubectl_get_namespaces,
    kubectl_get_resource_quotas,
)

scaling_analyzer_subagent = {
    "name": "scaling-analyzer",
    "description": (
        "Analyze horizontal pod autoscaling, replica counts, and workload sizing. "
        "Identifies under-scaled, over-scaled, or misconfigured HPAs. Checks node "
        "capacity vs workload demand. Returns sizing recommendations."
    ),
    "system_prompt": (
        "You are a Kubernetes scaling and sizing specialist. When asked to analyze scaling:\n"
        "1. List deployments and check desired vs ready vs available replicas\n"
        "2. Check all HPAs — note current vs min/max replicas and CPU utilization\n"
        "3. Run kubectl top pods and top nodes to get live resource usage\n"
        "4. Identify issues:\n"
        "   - Deployments stuck at 0 replicas\n"
        "   - HPAs at max replicas (may need higher max or more nodes)\n"
        "   - HPAs at min replicas with low utilization (may be over-provisioned)\n"
        "   - Deployments without HPA that have variable traffic patterns\n"
        "   - Nodes at >80% CPU or memory (risk of evictions)\n"
        "5. Return a structured report:\n"
        "   - SCALING ISSUES: list each problem\n"
        "   - NODE CAPACITY: current utilization vs capacity\n"
        "   - RECOMMENDATIONS: specific changes with exact values\n"
        "     (e.g. 'Increase max replicas of payments-api from 5 to 10')\n"
        "     (e.g. 'Set HPA target CPU from 80% to 60% for web-frontend')\n"
        "Include specific numbers and resource names in all recommendations."
    ),
    "tools": [
        kubectl_get_namespaces,
        kubectl_get_deployments,
        kubectl_describe_deployment,
        kubectl_get_hpa,
        kubectl_top_pods,
        kubectl_top_nodes,
        kubectl_get_resource_quotas,
    ],
}
