"""Configuration hygiene and resource health Kubernetes read tools."""
from __future__ import annotations
import traceback
from datetime import datetime, timezone
from kubernetes.client.rest import ApiException
from langchain.tools import tool
from .k8s_client import core_v1, apps_v1


def _safe(fn):
    try:
        return fn()
    except ApiException as e:
        return f"ERROR [{e.status}]: {e.reason}"
    except Exception:
        return f"ERROR: {traceback.format_exc(limit=3)}"


def _age(ts) -> str:
    if ts is None:
        return "unknown"
    now = datetime.now(timezone.utc)
    delta = now - ts
    s = int(delta.total_seconds())
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


@tool
def kubectl_audit_missing_limits(namespace: str = "default") -> str:
    """
    Find containers in deployments and statefulsets that are missing resource requests
    or limits. Missing requests cause poor scheduling; missing limits allow noisy neighbors.
    Pass namespace='--all-namespaces' to scan cluster-wide.
    """
    def _run():
        if namespace == "--all-namespaces":
            deployments = apps_v1().list_deployment_for_all_namespaces().items
            statefulsets = apps_v1().list_stateful_set_for_all_namespaces().items
        else:
            deployments = apps_v1().list_namespaced_deployment(namespace).items
            statefulsets = apps_v1().list_namespaced_stateful_set(namespace).items

        no_requests = []
        no_limits = []
        no_both = []

        for workload, kind in [(d, "Deployment") for d in deployments] + \
                              [(s, "StatefulSet") for s in statefulsets]:
            ns = workload.metadata.namespace
            name = workload.metadata.name
            for c in (workload.spec.template.spec.containers or []):
                r = c.resources
                missing_req = not r or not r.requests
                missing_lim = not r or not r.limits
                ref = f"  {kind} {ns}/{name} container='{c.name}'"
                if missing_req and missing_lim:
                    no_both.append(ref)
                elif missing_req:
                    no_requests.append(ref)
                elif missing_lim:
                    no_limits.append(ref)

        out = []
        if no_both:
            out.append(f"=== MISSING BOTH REQUESTS AND LIMITS ({len(no_both)}) ===")
            out.extend(no_both)
        if no_requests:
            out.append(f"\n=== MISSING REQUESTS ONLY ({len(no_requests)}) ===")
            out.extend(no_requests)
        if no_limits:
            out.append(f"\n=== MISSING LIMITS ONLY ({len(no_limits)}) ===")
            out.extend(no_limits)

        if not out:
            scope = namespace if namespace != "--all-namespaces" else "all namespaces"
            return f"All containers in {scope} have resource requests and limits set."
        return "\n".join(out)
    return _safe(_run)


@tool
def kubectl_get_pvs() -> str:
    """
    List all PersistentVolumes cluster-wide — shows capacity, access modes, reclaim policy,
    status, and bound claim. Flags Released and Failed PVs (likely orphaned / wasting storage).
    """
    def _run():
        pvs = core_v1().list_persistent_volume().items
        if not pvs:
            return "No PersistentVolumes found in cluster."

        lines = [f"{'NAME':<35} {'CAPACITY':<10} {'ACCESS':<15} {'RECLAIM':<10} {'STATUS':<10} CLAIM"]
        orphaned = []

        for pv in sorted(pvs, key=lambda x: x.metadata.name):
            name = pv.metadata.name
            capacity = (pv.spec.capacity or {}).get("storage", "?")
            access = ",".join(pv.spec.access_modes or [])
            reclaim = pv.spec.persistent_volume_reclaim_policy or "?"
            status = pv.status.phase or "Unknown"
            claim = ""
            if pv.spec.claim_ref:
                claim = f"{pv.spec.claim_ref.namespace}/{pv.spec.claim_ref.name}"

            if status in ("Released", "Failed"):
                orphaned.append(f"  {name}: {status}, capacity={capacity}, reclaim={reclaim}")

            lines.append(f"{name:<35} {capacity:<10} {access:<15} {reclaim:<10} {status:<10} {claim}")

        out = "\n".join(lines)
        if orphaned:
            out += "\n\n=== ORPHANED / WASTED PVs (Released or Failed) ===\n" + "\n".join(orphaned)
        return out
    return _safe(_run)


@tool
def kubectl_get_limit_ranges(namespace: str = "default") -> str:
    """
    List LimitRange objects — shows default requests/limits applied to containers
    when not explicitly set. Helps understand implicit constraints in a namespace.
    Pass namespace='--all-namespaces' to scan cluster-wide.
    """
    def _run():
        if namespace == "--all-namespaces":
            items = core_v1().list_limit_range_for_all_namespaces().items
        else:
            items = core_v1().list_namespaced_limit_range(namespace).items

        if not items:
            scope = namespace if namespace != "--all-namespaces" else "cluster"
            return f"No LimitRanges found in {scope}."

        out = []
        for lr in items:
            out.append(f"\nLimitRange: {lr.metadata.namespace}/{lr.metadata.name}")
            for limit in (lr.spec.limits or []):
                out.append(f"  Type: {limit.type}")
                if limit.default:
                    out.append(f"    Default (limits):   {limit.default}")
                if limit.default_request:
                    out.append(f"    DefaultRequest:     {limit.default_request}")
                if limit.max:
                    out.append(f"    Max:                {limit.max}")
                if limit.min:
                    out.append(f"    Min:                {limit.min}")
        return "\n".join(out)
    return _safe(_run)


@tool
def kubectl_audit_selector_mismatch(namespace: str = "default") -> str:
    """
    Find Services whose label selectors don't match any running pod labels.
    These services are traffic blackholes even if endpoints exist from a prior deployment.
    Pass namespace='--all-namespaces' to scan cluster-wide.
    """
    def _run():
        if namespace == "--all-namespaces":
            services = core_v1().list_service_for_all_namespaces().items
        else:
            services = core_v1().list_namespaced_service(namespace).items

        mismatches = []
        headless = []

        for svc in services:
            ns = svc.metadata.namespace
            name = svc.metadata.name
            selector = svc.spec.selector

            # Skip headless/ExternalName services with no selector
            if not selector:
                if svc.spec.type != "ExternalName":
                    headless.append(f"  {ns}/{name}: no selector (headless or manually managed endpoints)")
                continue

            # Get pods in the same namespace
            pods = core_v1().list_namespaced_pod(ns).items
            match = any(
                all(pod.metadata.labels.get(k) == v for k, v in selector.items())
                for pod in pods
                if pod.metadata.labels
            )
            if not match:
                mismatches.append(f"  {ns}/{name}: selector={selector}")

        out = []
        if mismatches:
            out.append(f"=== SELECTOR MISMATCHES — no matching pods ({len(mismatches)}) ===")
            out.extend(mismatches)
        else:
            out.append("=== SELECTOR MISMATCHES: none found ===")

        if headless:
            out.append(f"\n=== SERVICES WITH NO SELECTOR (headless/manual) ===")
            out.extend(headless)

        return "\n".join(out)
    return _safe(_run)
