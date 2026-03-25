"""Security Auditor subagent — RBAC, pod security, network policies, image hygiene."""
from tools import (
    kubectl_get_namespaces,
    kubectl_get_rbac_summary,
    kubectl_audit_pod_security,
    kubectl_get_network_policies,
    kubectl_audit_image_tags,
)

security_auditor_subagent = {
    "name": "security-auditor",
    "description": (
        "Audit Kubernetes security posture. Checks RBAC for overly broad permissions and "
        "cluster-admin bindings, scans pods for privileged containers / hostNetwork / root "
        "execution, identifies namespaces with no NetworkPolicies, and flags :latest image "
        "tags or non-standard registries. Returns a structured security findings report."
    ),
    "system_prompt": (
        "You are a Kubernetes security specialist. When asked to audit security:\n"
        "1. Run kubectl_get_rbac_summary — flag cluster-admin bindings and wildcard roles\n"
        "2. Run kubectl_audit_pod_security with namespace='--all-namespaces'\n"
        "3. Run kubectl_get_network_policies with namespace='--all-namespaces' — flag unprotected namespaces\n"
        "4. Run kubectl_audit_image_tags with namespace='--all-namespaces'\n"
        "5. Return a structured report:\n"
        "   - CRITICAL: privileged containers, cluster-admin granted to unexpected subjects, "
        "hostNetwork/hostPID pods\n"
        "   - WARNING: wildcard RBAC roles, :latest images, namespaces with no NetworkPolicy\n"
        "   - INFO: non-standard registries, containers without securityContext\n"
        "   - RECOMMENDATIONS: specific remediation for each finding\n"
        "Be specific — include names, namespaces, and exact misconfiguration details."
    ),
    "tools": [
        kubectl_get_namespaces,
        kubectl_get_rbac_summary,
        kubectl_audit_pod_security,
        kubectl_get_network_policies,
        kubectl_audit_image_tags,
    ],
}
