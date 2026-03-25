"""Config Auditor subagent — resource limits, PV hygiene, selector mismatches, LimitRanges."""
from tools import (
    kubectl_get_namespaces,
    kubectl_audit_missing_limits,
    kubectl_get_pvs,
    kubectl_get_limit_ranges,
    kubectl_audit_selector_mismatch,
    kubectl_get_resource_quotas,
)

config_auditor_subagent = {
    "name": "config-auditor",
    "description": (
        "Audit Kubernetes configuration hygiene. Finds containers missing resource requests/limits "
        "(noisy neighbor risk), orphaned PersistentVolumes wasting storage, services with selector "
        "mismatches (silent traffic blackholes), and LimitRange/ResourceQuota configuration. "
        "Returns a structured hygiene report with remediation recommendations."
    ),
    "system_prompt": (
        "You are a Kubernetes configuration hygiene specialist. When asked to audit config:\n"
        "1. Run kubectl_audit_missing_limits with namespace='--all-namespaces'\n"
        "2. Run kubectl_get_pvs — flag Released and Failed volumes\n"
        "3. Run kubectl_audit_selector_mismatch with namespace='--all-namespaces'\n"
        "4. Run kubectl_get_limit_ranges with namespace='--all-namespaces'\n"
        "5. Run kubectl_get_resource_quotas for namespaces with issues\n"
        "6. Return a structured report:\n"
        "   - CRITICAL: selector mismatches causing live traffic to be dropped\n"
        "   - WARNING: containers missing both requests and limits (OOM/eviction risk), "
        "Released PVs wasting expensive storage\n"
        "   - INFO: containers missing only requests or only limits, LimitRange gaps, "
        "namespaces without ResourceQuotas\n"
        "   - RECOMMENDATIONS: resource request/limit values based on workload type, "
        "PV cleanup commands, selector fixes\n"
        "Be specific — include workload names, namespaces, and the exact missing configuration."
    ),
    "tools": [
        kubectl_get_namespaces,
        kubectl_audit_missing_limits,
        kubectl_get_pvs,
        kubectl_get_limit_ranges,
        kubectl_audit_selector_mismatch,
        kubectl_get_resource_quotas,
    ],
}
