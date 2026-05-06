"""Reliability Auditor subagent — PDBs, probes, endpoints, single-replica SPOFs."""
from config import SUBAGENT_MODEL
from tools import (
    kubectl_get_namespaces,
    kubectl_get_pdbs,
    kubectl_audit_probes,
    kubectl_get_endpoints,
    kubectl_audit_single_replicas,
    kubectl_get_deployments,
)

reliability_auditor_subagent = {
    "name": "reliability-auditor",
    "model": SUBAGENT_MODEL,
    "description": (
        "Audit Kubernetes reliability and resilience. Identifies workloads missing "
        "PodDisruptionBudgets (unprotected during node drains), containers without "
        "liveness/readiness probes, services with zero ready endpoints (traffic blackholes), "
        "and single-replica deployments (SPOFs). Returns a structured resilience report."
    ),
    "system_prompt": (
        "You are a Kubernetes reliability specialist. When asked to audit reliability:\n"
        "1. Run kubectl_get_pdbs with namespace='--all-namespaces' — identify unprotected workloads\n"
        "2. Run kubectl_audit_probes with namespace='--all-namespaces' — find missing health probes\n"
        "3. Run kubectl_get_endpoints with namespace='--all-namespaces' — find traffic blackholes\n"
        "4. Run kubectl_audit_single_replicas with namespace='--all-namespaces' — identify SPOFs\n"
        "5. Return a structured report:\n"
        "   - CRITICAL: services with 0 ready endpoints (live traffic impact)\n"
        "   - WARNING: single-replica deployments with no PDB, missing readiness probes on "
        "services receiving traffic\n"
        "   - INFO: missing liveness probes, multi-replica workloads without PDB\n"
        "   - RECOMMENDATIONS: add PDB templates, probe configurations, and replica count suggestions\n"
        "Be specific — include names, namespaces, and the exact gap in each finding."
    ),
    "tools": [
        kubectl_get_namespaces,
        kubectl_get_pdbs,
        kubectl_audit_probes,
        kubectl_get_endpoints,
        kubectl_audit_single_replicas,
        kubectl_get_deployments,
    ],
}
