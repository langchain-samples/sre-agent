"""Security-focused Kubernetes read tools."""
from __future__ import annotations
import traceback
from kubernetes.client.rest import ApiException
from langchain.tools import tool
from .k8s_client import core_v1, rbac_v1, networking_v1


def _safe(fn):
    try:
        return fn()
    except ApiException as e:
        return f"ERROR [{e.status}]: {e.reason}"
    except Exception:
        return f"ERROR: {traceback.format_exc(limit=3)}"


@tool
def kubectl_get_rbac_summary() -> str:
    """
    Summarize RBAC posture: list all ClusterRoleBindings, flag cluster-admin bindings,
    and list ClusterRoles with wildcard verbs or resources (overly broad permissions).
    """
    def _run():
        out = []

        # ClusterRoleBindings — who has what at cluster scope
        crbs = rbac_v1().list_cluster_role_binding().items
        out.append("=== CLUSTERROLEBINDINGS ===")
        out.append(f"{'BINDING':<40} {'ROLE':<35} SUBJECTS")
        for crb in sorted(crbs, key=lambda x: x.metadata.name):
            role = crb.role_ref.name if crb.role_ref else "?"
            subjects = ", ".join(
                f"{s.kind}/{s.name}" + (f" ({s.namespace})" if s.namespace else "")
                for s in (crb.subjects or [])
            ) or "<none>"
            flag = " *** CLUSTER-ADMIN ***" if role == "cluster-admin" else ""
            out.append(f"{crb.metadata.name:<40} {role:<35} {subjects}{flag}")

        # ClusterRoles with wildcard verbs or resources
        out.append("\n=== OVERLY BROAD CLUSTERROLES (wildcard verb or resource) ===")
        crs = rbac_v1().list_cluster_role().items
        flagged = []
        for cr in crs:
            for rule in (cr.rules or []):
                verbs = rule.verbs or []
                resources = rule.resources or []
                if "*" in verbs or "*" in resources:
                    flagged.append(
                        f"  {cr.metadata.name}: verbs={verbs}, resources={resources}, "
                        f"apiGroups={rule.api_groups or []}"
                    )
                    break
        if flagged:
            out.extend(flagged)
        else:
            out.append("  None found.")

        # Namespace-scoped RoleBindings granting cluster-admin equivalent
        out.append("\n=== ROLEBINDINGS REFERENCING CLUSTERROLES ===")
        rbs = rbac_v1().list_role_binding_for_all_namespaces().items
        cluster_role_refs = [rb for rb in rbs if rb.role_ref.kind == "ClusterRole"]
        if cluster_role_refs:
            out.append(f"{'NAMESPACE':<20} {'BINDING':<35} {'CLUSTERROLE':<30} SUBJECTS")
            for rb in sorted(cluster_role_refs, key=lambda x: (x.metadata.namespace, x.metadata.name)):
                subjects = ", ".join(
                    f"{s.kind}/{s.name}" for s in (rb.subjects or [])
                ) or "<none>"
                out.append(
                    f"{rb.metadata.namespace:<20} {rb.metadata.name:<35} "
                    f"{rb.role_ref.name:<30} {subjects}"
                )
        else:
            out.append("  None found.")

        return "\n".join(out)
    return _safe(_run)


@tool
def kubectl_audit_pod_security(namespace: str = "default") -> str:
    """
    Audit pods for security misconfigurations: privileged containers, hostNetwork/hostPID/hostIPC,
    containers running as root, missing securityContext, and allowPrivilegeEscalation.
    Pass namespace='--all-namespaces' to scan cluster-wide.
    """
    def _run():
        if namespace == "--all-namespaces":
            pods = core_v1().list_pod_for_all_namespaces().items
        else:
            pods = core_v1().list_namespaced_pod(namespace).items

        findings = []
        for p in pods:
            ns = p.metadata.namespace
            name = p.metadata.name
            spec = p.spec
            pod_issues = []

            # Pod-level flags
            if spec.host_network:
                pod_issues.append("hostNetwork=true")
            if spec.host_pid:
                pod_issues.append("hostPID=true")
            if spec.host_ipc:
                pod_issues.append("hostIPC=true")

            # Container-level flags
            for c in (spec.containers or []) + (spec.init_containers or []):
                sc = c.security_context
                if sc is None:
                    pod_issues.append(f"container '{c.name}': no securityContext")
                    continue
                if sc.privileged:
                    pod_issues.append(f"container '{c.name}': privileged=true")
                if sc.allow_privilege_escalation is True:
                    pod_issues.append(f"container '{c.name}': allowPrivilegeEscalation=true")
                if sc.run_as_user == 0:
                    pod_issues.append(f"container '{c.name}': runAsUser=0 (root)")
                if sc.run_as_non_root is False:
                    pod_issues.append(f"container '{c.name}': runAsNonRoot=false")

            if pod_issues:
                findings.append(f"\n{ns}/{name}:")
                for issue in pod_issues:
                    findings.append(f"  - {issue}")

        if not findings:
            scope = namespace if namespace != "--all-namespaces" else "all namespaces"
            return f"No pod security issues found in {scope}."
        return "=== POD SECURITY AUDIT ===" + "".join(findings)
    return _safe(_run)


@tool
def kubectl_get_network_policies(namespace: str = "default") -> str:
    """
    List NetworkPolicies and identify namespaces with no policies (fully open traffic).
    Pass namespace='--all-namespaces' to scan cluster-wide.
    """
    def _run():
        out = []

        if namespace == "--all-namespaces":
            policies = networking_v1().list_network_policy_for_all_namespaces().items
            all_ns = [ns.metadata.name for ns in core_v1().list_namespace().items]
            ns_with_policies = set(p.metadata.namespace for p in policies)
            unprotected = [ns for ns in all_ns if ns not in ns_with_policies]

            out.append("=== NETWORK POLICIES ===")
            out.append(f"{'NAMESPACE':<25} {'NAME':<35} POD SELECTOR")
            for p in sorted(policies, key=lambda x: (x.metadata.namespace, x.metadata.name)):
                sel = str(p.spec.pod_selector.match_labels or "{}") if p.spec.pod_selector else "{}"
                out.append(f"{p.metadata.namespace:<25} {p.metadata.name:<35} {sel}")

            if unprotected:
                out.append(f"\n=== NAMESPACES WITH NO NETWORK POLICIES ({len(unprotected)}) ===")
                for ns in sorted(unprotected):
                    out.append(f"  {ns}  *** NO NETWORK POLICIES — unrestricted ingress/egress ***")
            else:
                out.append("\nAll namespaces have at least one NetworkPolicy.")
        else:
            policies = networking_v1().list_namespaced_network_policy(namespace).items
            if not policies:
                out.append(f"WARNING: No NetworkPolicies in namespace '{namespace}' — unrestricted traffic.")
            else:
                out.append(f"{'NAME':<35} POD SELECTOR          POLICY TYPES")
                for p in policies:
                    sel = str(p.spec.pod_selector.match_labels or "{}") if p.spec.pod_selector else "{}"
                    types = ",".join(p.spec.policy_types or [])
                    out.append(f"{p.metadata.name:<35} {sel:<21} {types}")

        return "\n".join(out)
    return _safe(_run)


@tool
def kubectl_audit_image_tags(namespace: str = "default") -> str:
    """
    Scan all running pods for risky image tags: ':latest', missing tags (implicit latest),
    and images from non-standard registries.
    Pass namespace='--all-namespaces' to scan cluster-wide.
    Standard registries: docker.io, gcr.io, ghcr.io, quay.io, registry.k8s.io, public.ecr.aws.
    """
    def _run():
        STANDARD_REGISTRIES = {
            "docker.io", "gcr.io", "ghcr.io", "quay.io",
            "registry.k8s.io", "public.ecr.aws", "k8s.gcr.io",
        }

        if namespace == "--all-namespaces":
            pods = core_v1().list_pod_for_all_namespaces().items
        else:
            pods = core_v1().list_namespaced_pod(namespace).items

        latest_tags = []
        non_standard = []

        for p in pods:
            ns = p.metadata.namespace
            name = p.metadata.name
            for c in (p.spec.containers or []) + (p.spec.init_containers or []):
                image = c.image or ""
                # Detect :latest or no tag
                img_name = image.split("@")[0]  # strip digest
                if ":" not in img_name.split("/")[-1] or img_name.endswith(":latest"):
                    latest_tags.append(f"  {ns}/{name}  container='{c.name}'  image={image}")
                # Detect non-standard registry
                parts = image.split("/")
                if len(parts) >= 2 and "." in parts[0]:
                    registry = parts[0]
                    if not any(registry.endswith(r) for r in STANDARD_REGISTRIES):
                        non_standard.append(
                            f"  {ns}/{name}  container='{c.name}'  registry={registry}  image={image}"
                        )

        out = []
        if latest_tags:
            out.append(f"=== :LATEST OR UNTAGGED IMAGES ({len(latest_tags)}) ===")
            out.extend(latest_tags)
        else:
            out.append("=== :LATEST OR UNTAGGED IMAGES: none found ===")

        if non_standard:
            out.append(f"\n=== NON-STANDARD REGISTRIES ({len(non_standard)}) ===")
            out.extend(non_standard)
        else:
            out.append("\n=== NON-STANDARD REGISTRIES: none found ===")

        return "\n".join(out)
    return _safe(_run)
