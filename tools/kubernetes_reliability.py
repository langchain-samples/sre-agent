"""Reliability and resilience Kubernetes read tools."""
from __future__ import annotations
import traceback
from kubernetes.client.rest import ApiException
from langchain.tools import tool
from .k8s_client import core_v1, apps_v1, policy_v1


def _safe(fn):
    try:
        return fn()
    except ApiException as e:
        return f"ERROR [{e.status}]: {e.reason}"
    except Exception:
        return f"ERROR: {traceback.format_exc(limit=3)}"


@tool
def kubectl_get_pdbs(namespace: str = "default") -> str:
    """
    List PodDisruptionBudgets — shows min available, max unavailable, current/desired/allowed
    disruptions. Identifies deployments/statefulsets with no PDB (unprotected during node drains).
    Pass namespace='--all-namespaces' to scan cluster-wide.
    """
    def _run():
        if namespace == "--all-namespaces":
            pdbs = policy_v1().list_pod_disruption_budget_for_all_namespaces().items
            deps_by_ns: dict[str, list] = {}
            for d in apps_v1().list_deployment_for_all_namespaces().items:
                deps_by_ns.setdefault(d.metadata.namespace, []).append(d)
            sts_by_ns: dict[str, list] = {}
            for s in apps_v1().list_stateful_set_for_all_namespaces().items:
                sts_by_ns.setdefault(s.metadata.namespace, []).append(s)
        else:
            pdbs = policy_v1().list_namespaced_pod_disruption_budget(namespace).items
            deps_by_ns = {namespace: apps_v1().list_namespaced_deployment(namespace).items}
            sts_by_ns = {namespace: apps_v1().list_namespaced_stateful_set(namespace).items}

        out = []
        if pdbs:
            out.append(f"{'NAMESPACE':<20} {'NAME':<30} {'SELECTOR':<35} MIN-AVAIL   MAX-UNAVAIL   DISRUPTIONS-ALLOWED")
            for pdb in sorted(pdbs, key=lambda x: (x.metadata.namespace, x.metadata.name)):
                sel = str(pdb.spec.selector.match_labels or {}) if pdb.spec.selector else "{}"
                min_a = str(pdb.spec.min_available) if pdb.spec.min_available is not None else "-"
                max_u = str(pdb.spec.max_unavailable) if pdb.spec.max_unavailable is not None else "-"
                allowed = pdb.status.disruptions_allowed if pdb.status else "?"
                out.append(
                    f"{pdb.metadata.namespace:<20} {pdb.metadata.name:<30} {sel:<35} "
                    f"{min_a:<11} {max_u:<13} {allowed}"
                )
        else:
            scope = namespace if namespace != "--all-namespaces" else "cluster"
            out.append(f"No PodDisruptionBudgets found in {scope}.")

        # Find workloads with >1 replica and no matching PDB
        pdb_namespaces = set(p.metadata.namespace for p in pdbs)
        out.append("\n=== WORKLOADS WITHOUT PDB (replicas > 1) ===")
        unprotected = []
        for ns, deps in deps_by_ns.items():
            ns_pdbs = [p for p in pdbs if p.metadata.namespace == ns]
            for d in deps:
                if (d.spec.replicas or 1) <= 1:
                    continue
                labels = d.spec.template.metadata.labels or {}
                covered = any(
                    all(labels.get(k) == v for k, v in (p.spec.selector.match_labels or {}).items())
                    for p in ns_pdbs
                    if p.spec.selector and p.spec.selector.match_labels
                )
                if not covered:
                    unprotected.append(f"  Deployment {ns}/{d.metadata.name} ({d.spec.replicas} replicas)")
        for ns, sts_list in sts_by_ns.items():
            ns_pdbs = [p for p in pdbs if p.metadata.namespace == ns]
            for s in sts_list:
                if (s.spec.replicas or 1) <= 1:
                    continue
                labels = s.spec.template.metadata.labels or {}
                covered = any(
                    all(labels.get(k) == v for k, v in (p.spec.selector.match_labels or {}).items())
                    for p in ns_pdbs
                    if p.spec.selector and p.spec.selector.match_labels
                )
                if not covered:
                    unprotected.append(f"  StatefulSet {ns}/{s.metadata.name} ({s.spec.replicas} replicas)")

        if unprotected:
            out.extend(unprotected)
        else:
            out.append("  All multi-replica workloads are covered by a PDB.")

        return "\n".join(out)
    return _safe(_run)


@tool
def kubectl_audit_probes(namespace: str = "default") -> str:
    """
    Audit deployments and statefulsets for missing liveness and readiness probes.
    Missing probes mean pods can't self-heal (liveness) or are sent traffic before ready (readiness).
    Pass namespace='--all-namespaces' to scan cluster-wide.
    """
    def _run():
        if namespace == "--all-namespaces":
            deployments = apps_v1().list_deployment_for_all_namespaces().items
            statefulsets = apps_v1().list_stateful_set_for_all_namespaces().items
        else:
            deployments = apps_v1().list_namespaced_deployment(namespace).items
            statefulsets = apps_v1().list_namespaced_stateful_set(namespace).items

        missing_liveness = []
        missing_readiness = []
        missing_both = []

        for workload, kind in [(d, "Deployment") for d in deployments] + \
                              [(s, "StatefulSet") for s in statefulsets]:
            ns = workload.metadata.namespace
            name = workload.metadata.name
            containers = workload.spec.template.spec.containers or []
            for c in containers:
                no_live = c.liveness_probe is None
                no_ready = c.readiness_probe is None
                ref = f"{kind} {ns}/{name} container='{c.name}'"
                if no_live and no_ready:
                    missing_both.append(f"  {ref}")
                elif no_live:
                    missing_liveness.append(f"  {ref}")
                elif no_ready:
                    missing_readiness.append(f"  {ref}")

        out = []
        if missing_both:
            out.append(f"=== MISSING BOTH PROBES ({len(missing_both)}) ===")
            out.extend(missing_both)
        if missing_liveness:
            out.append(f"\n=== MISSING LIVENESS PROBE ONLY ({len(missing_liveness)}) ===")
            out.extend(missing_liveness)
        if missing_readiness:
            out.append(f"\n=== MISSING READINESS PROBE ONLY ({len(missing_readiness)}) ===")
            out.extend(missing_readiness)

        total = len(missing_both) + len(missing_liveness) + len(missing_readiness)
        if total == 0:
            scope = namespace if namespace != "--all-namespaces" else "all namespaces"
            return f"All containers in {scope} have liveness and readiness probes configured."

        return "\n".join(out)
    return _safe(_run)


@tool
def kubectl_get_endpoints(namespace: str = "default") -> str:
    """
    List services and their endpoint health — shows ready vs not-ready endpoint counts.
    Flags services with 0 ready endpoints (traffic blackhole).
    Pass namespace='--all-namespaces' to scan cluster-wide.
    """
    def _run():
        if namespace == "--all-namespaces":
            endpoints = core_v1().list_endpoints_for_all_namespaces().items
        else:
            endpoints = core_v1().list_namespaced_endpoints(namespace).items

        lines = [f"{'NAMESPACE':<20} {'SERVICE':<35} {'READY':<8} {'NOT-READY':<11} STATUS"]
        blackholes = []

        for ep in sorted(endpoints, key=lambda x: (x.metadata.namespace, x.metadata.name)):
            ns = ep.metadata.namespace
            name = ep.metadata.name
            ready = 0
            not_ready = 0
            for subset in (ep.subsets or []):
                ready += len(subset.addresses or [])
                not_ready += len(subset.not_ready_addresses or [])
            status = "OK"
            if ready == 0 and not_ready == 0:
                status = "NO ENDPOINTS"
            elif ready == 0:
                status = "*** ALL NOT-READY ***"
                blackholes.append(f"  {ns}/{name}")
            elif not_ready > 0:
                status = f"degraded ({not_ready} not-ready)"
            lines.append(f"{ns:<20} {name:<35} {ready:<8} {not_ready:<11} {status}")

        out = "\n".join(lines)
        if blackholes:
            out += "\n\n=== TRAFFIC BLACKHOLES (0 ready endpoints) ===\n" + "\n".join(blackholes)
        return out
    return _safe(_run)


@tool
def kubectl_audit_single_replicas(namespace: str = "default") -> str:
    """
    Find deployments running with only 1 replica — these are single points of failure.
    Cross-references PodDisruptionBudgets to identify which are also unprotected during drains.
    Pass namespace='--all-namespaces' to scan cluster-wide.
    """
    def _run():
        if namespace == "--all-namespaces":
            deployments = apps_v1().list_deployment_for_all_namespaces().items
            pdbs = policy_v1().list_pod_disruption_budget_for_all_namespaces().items
        else:
            deployments = apps_v1().list_namespaced_deployment(namespace).items
            pdbs = policy_v1().list_namespaced_pod_disruption_budget(namespace).items

        single_replica = []
        for d in deployments:
            if (d.spec.replicas or 1) == 1:
                ns = d.metadata.namespace
                name = d.metadata.name
                labels = d.spec.template.metadata.labels or {}
                ns_pdbs = [p for p in pdbs if p.metadata.namespace == ns]
                has_pdb = any(
                    all(labels.get(k) == v for k, v in (p.spec.selector.match_labels or {}).items())
                    for p in ns_pdbs
                    if p.spec.selector and p.spec.selector.match_labels
                )
                single_replica.append((ns, name, has_pdb))

        if not single_replica:
            scope = namespace if namespace != "--all-namespaces" else "cluster"
            return f"No single-replica deployments found in {scope}."

        lines = [f"{'NAMESPACE':<20} {'DEPLOYMENT':<35} PDB?"]
        for ns, name, has_pdb in sorted(single_replica):
            pdb_str = "yes" if has_pdb else "NO — unprotected SPOF"
            lines.append(f"{ns:<20} {name:<35} {pdb_str}")
        return "\n".join(lines)
    return _safe(_run)
