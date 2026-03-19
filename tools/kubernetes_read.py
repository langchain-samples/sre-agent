"""Read-only Kubernetes tools using the Python kubernetes client."""
from __future__ import annotations
import traceback
from datetime import datetime, timezone
from typing import Optional
from langchain.tools import tool
from kubernetes.client.rest import ApiException
from .k8s_client import core_v1, apps_v1, autoscaling_v2, networking_v1, custom_objects, apiextensions_v1


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


def _safe(fn):
    """Wrap a function and return error string on ApiException."""
    try:
        return fn()
    except ApiException as e:
        return f"ERROR [{e.status}]: {e.reason}"
    except Exception:
        return f"ERROR: {traceback.format_exc(limit=3)}"


@tool
def kubectl_get_namespaces() -> str:
    """List all Kubernetes namespaces with their phase and age."""
    def _run():
        items = core_v1().list_namespace().items
        lines = ["NAME                         STATUS   AGE"]
        for ns in items:
            lines.append(
                f"{ns.metadata.name:<30} {ns.status.phase:<8} {_age(ns.metadata.creation_timestamp)}"
            )
        return "\n".join(lines)
    return _safe(_run)


@tool
def kubectl_get_nodes() -> str:
    """List all cluster nodes with status, roles, Kubernetes version, and resource capacity."""
    def _run():
        items = core_v1().list_node().items
        lines = ["NAME                     STATUS    ROLES    VERSION           CPU    MEMORY"]
        for n in items:
            conditions = {c.type: c.status for c in (n.status.conditions or [])}
            status = "Ready" if conditions.get("Ready") == "True" else "NotReady"
            roles = ",".join(
                k.replace("node-role.kubernetes.io/", "")
                for k in (n.metadata.labels or {})
                if k.startswith("node-role.kubernetes.io/")
            ) or "worker"
            version = n.status.node_info.kubelet_version if n.status.node_info else "unknown"
            cpu = n.status.capacity.get("cpu", "?") if n.status.capacity else "?"
            mem = n.status.capacity.get("memory", "?") if n.status.capacity else "?"
            lines.append(f"{n.metadata.name:<25} {status:<9} {roles:<8} {version:<17} {cpu:<6} {mem}")
        return "\n".join(lines)
    return _safe(_run)


@tool
def kubectl_get_pods(namespace: str = "default") -> str:
    """
    List all pods in a namespace (or all namespaces if namespace='--all-namespaces')
    with phase, restart count, and age.
    """
    def _run():
        if namespace == "--all-namespaces":
            items = core_v1().list_pod_for_all_namespaces().items
            lines = ["NAMESPACE            NAME                                      STATUS     RESTARTS   AGE"]
            for p in items:
                restarts = sum(
                    (cs.restart_count or 0) for cs in (p.status.container_statuses or [])
                )
                lines.append(
                    f"{p.metadata.namespace:<21} {p.metadata.name:<42} "
                    f"{p.status.phase or 'Unknown':<10} {restarts:<10} {_age(p.metadata.creation_timestamp)}"
                )
        else:
            items = core_v1().list_namespaced_pod(namespace).items
            lines = ["NAME                                      STATUS     RESTARTS   AGE"]
            for p in items:
                restarts = sum(
                    (cs.restart_count or 0) for cs in (p.status.container_statuses or [])
                )
                # Show container-level status for non-running pods
                reason = p.status.phase or "Unknown"
                for cs in (p.status.container_statuses or []):
                    if cs.state and cs.state.waiting:
                        reason = cs.state.waiting.reason or reason
                    elif cs.state and cs.state.terminated:
                        reason = cs.state.terminated.reason or reason
                lines.append(
                    f"{p.metadata.name:<42} {reason:<10} {restarts:<10} {_age(p.metadata.creation_timestamp)}"
                )
        return "\n".join(lines)
    return _safe(_run)


@tool
def kubectl_describe_pod(pod_name: str, namespace: str = "default") -> str:
    """
    Describe a specific pod — returns conditions, container statuses,
    resource requests/limits, restart history, and recent events.
    """
    def _run():
        p = core_v1().read_namespaced_pod(pod_name, namespace)
        out = [f"Name: {p.metadata.name}", f"Namespace: {p.metadata.namespace}",
               f"Phase: {p.status.phase}", f"Node: {p.spec.node_name}",
               f"Created: {p.metadata.creation_timestamp}"]

        # Conditions
        if p.status.conditions:
            out.append("\nConditions:")
            for c in p.status.conditions:
                out.append(f"  {c.type}: {c.status} — {c.message or ''}")

        # Containers
        out.append("\nContainers:")
        for c in (p.spec.containers or []):
            out.append(f"  {c.name}:")
            if c.resources:
                out.append(f"    Requests: {c.resources.requests}")
                out.append(f"    Limits:   {c.resources.limits}")

        # Container statuses
        if p.status.container_statuses:
            out.append("\nContainer Statuses:")
            for cs in p.status.container_statuses:
                out.append(f"  {cs.name}: ready={cs.ready}, restarts={cs.restart_count}")
                if cs.state:
                    if cs.state.running:
                        out.append(f"    Running since {cs.state.running.started_at}")
                    if cs.state.waiting:
                        out.append(f"    Waiting: {cs.state.waiting.reason} — {cs.state.waiting.message or ''}")
                    if cs.state.terminated:
                        t = cs.state.terminated
                        out.append(f"    Terminated: {t.reason}, exit={t.exit_code}, msg={t.message or ''}")
                if cs.last_state and cs.last_state.terminated:
                    t = cs.last_state.terminated
                    out.append(f"    LastTerminated: {t.reason}, exit={t.exit_code} at {t.finished_at}")

        # Events
        events = core_v1().list_namespaced_event(
            namespace,
            field_selector=f"involvedObject.name={pod_name}"
        ).items
        if events:
            out.append("\nEvents:")
            for e in sorted(events, key=lambda x: x.last_timestamp or datetime.min.replace(tzinfo=timezone.utc)):
                out.append(f"  [{e.type}] {e.reason}: {e.message}  ({_age(e.last_timestamp)} ago)")

        return "\n".join(out)
    return _safe(_run)


@tool
def kubectl_get_pod_logs(
    pod_name: str,
    namespace: str = "default",
    container: str = "",
    tail_lines: int = 100,
    previous: bool = False,
) -> str:
    """
    Fetch logs from a pod container.
    Set previous=True to get logs from the previously terminated container
    (useful for diagnosing CrashLoopBackOff).
    """
    def _run():
        kwargs: dict = dict(
            name=pod_name,
            namespace=namespace,
            tail_lines=tail_lines,
            previous=previous,
        )
        if container:
            kwargs["container"] = container
        logs = core_v1().read_namespaced_pod_log(**kwargs)
        return logs or "(no logs)"
    return _safe(_run)


@tool
def kubectl_get_deployments(namespace: str = "default") -> str:
    """List all deployments — shows desired, ready, up-to-date, and available replicas."""
    def _run():
        items = apps_v1().list_namespaced_deployment(namespace).items
        lines = ["NAME                          DESIRED   READY   UP-TO-DATE   AVAILABLE   AGE"]
        for d in items:
            s = d.status
            lines.append(
                f"{d.metadata.name:<30} {d.spec.replicas or 0:<9} {s.ready_replicas or 0:<7} "
                f"{s.updated_replicas or 0:<12} {s.available_replicas or 0:<11} "
                f"{_age(d.metadata.creation_timestamp)}"
            )
        return "\n".join(lines)
    return _safe(_run)


@tool
def kubectl_describe_deployment(deployment_name: str, namespace: str = "default") -> str:
    """
    Describe a deployment — shows replicas, strategy, container specs,
    resource limits, and rollout conditions.
    """
    def _run():
        d = apps_v1().read_namespaced_deployment(deployment_name, namespace)
        out = [
            f"Name: {d.metadata.name}",
            f"Namespace: {d.metadata.namespace}",
            f"Replicas: desired={d.spec.replicas}, ready={d.status.ready_replicas}, "
            f"available={d.status.available_replicas}",
            f"Strategy: {d.spec.strategy.type if d.spec.strategy else 'unknown'}",
        ]
        out.append("\nContainers:")
        for c in (d.spec.template.spec.containers or []):
            out.append(f"  {c.name}  image={c.image}")
            if c.resources:
                out.append(f"    Requests: {c.resources.requests}")
                out.append(f"    Limits:   {c.resources.limits}")
        if d.status.conditions:
            out.append("\nConditions:")
            for cond in d.status.conditions:
                out.append(f"  {cond.type}: {cond.status} — {cond.message or ''}")
        return "\n".join(out)
    return _safe(_run)


@tool
def kubectl_get_hpa(namespace: str = "default") -> str:
    """
    List HorizontalPodAutoscalers — shows min/max replicas, current replicas,
    and target vs current CPU utilization.
    """
    def _run():
        items = autoscaling_v2().list_namespaced_horizontal_pod_autoscaler(namespace).items
        if not items:
            return f"No HPAs found in namespace '{namespace}'"
        lines = ["NAME                    TARGET              MIN   MAX   REPLICAS   CPU%    AGE"]
        for h in items:
            target = h.spec.scale_target_ref.name if h.spec.scale_target_ref else "?"
            min_r = h.spec.min_replicas or 1
            max_r = h.spec.max_replicas or "?"
            current = h.status.current_replicas or 0
            # Find CPU metric
            cpu_str = "?"
            if h.status.current_metrics:
                for m in h.status.current_metrics:
                    if m.type == "Resource" and m.resource and m.resource.name == "cpu":
                        util = m.resource.current.average_utilization
                        cpu_str = f"{util}%" if util is not None else "?"
            lines.append(
                f"{h.metadata.name:<24} {target:<19} {min_r:<5} {max_r:<5} "
                f"{current:<10} {cpu_str:<7} {_age(h.metadata.creation_timestamp)}"
            )
        return "\n".join(lines)
    return _safe(_run)


@tool
def kubectl_get_events(namespace: str = "default", warning_only: bool = False) -> str:
    """
    Get recent Kubernetes events sorted by time.
    Set warning_only=True to show only Warning events.
    """
    def _run():
        field_selector = "type=Warning" if warning_only else ""
        kwargs: dict = dict(namespace=namespace)
        if field_selector:
            kwargs["field_selector"] = field_selector
        items = core_v1().list_namespaced_event(**kwargs).items
        items.sort(key=lambda e: e.last_timestamp or datetime.min.replace(tzinfo=timezone.utc))
        lines = ["TYPE      REASON              OBJECT                            MESSAGE"]
        for e in items[-50:]:  # last 50 events
            obj = f"{e.involved_object.kind}/{e.involved_object.name}"
            msg = (e.message or "")[:80]
            lines.append(f"{e.type or '?':<9} {e.reason or '?':<19} {obj:<33} {msg}")
        return "\n".join(lines) if len(lines) > 1 else f"No events in namespace '{namespace}'"
    return _safe(_run)


@tool
def kubectl_get_services(namespace: str = "default") -> str:
    """List all services — shows type, cluster IP, external IP, and ports."""
    def _run():
        items = core_v1().list_namespaced_service(namespace).items
        lines = ["NAME                      TYPE          CLUSTER-IP      EXTERNAL-IP     PORT(S)"]
        for s in items:
            ports = ",".join(
                f"{p.port}/{p.protocol}" + (f":{p.node_port}" if p.node_port else "")
                for p in (s.spec.ports or [])
            )
            ext_ip = ",".join(
                (i.ip or i.hostname or "") for i in (s.status.load_balancer.ingress or [])
            ) if s.status.load_balancer else "<none>"
            lines.append(
                f"{s.metadata.name:<26} {s.spec.type:<13} "
                f"{s.spec.cluster_ip or '<none>':<15} {ext_ip:<15} {ports}"
            )
        return "\n".join(lines)
    return _safe(_run)


@tool
def kubectl_get_ingress(namespace: str = "default") -> str:
    """List all ingress resources — shows hosts, paths, and backends."""
    def _run():
        items = networking_v1().list_namespaced_ingress(namespace).items
        if not items:
            return f"No ingress resources in namespace '{namespace}'"
        lines = ["NAME                    CLASS       HOSTS                               ADDRESS"]
        for i in items:
            hosts = ",".join(
                rule.host or "*"
                for rule in (i.spec.rules or [])
            ) or "*"
            ingress_class = i.spec.ingress_class_name or (
                (i.metadata.annotations or {}).get("kubernetes.io/ingress.class", "<none>")
            )
            addr = ",".join(
                (lb.ip or lb.hostname or "")
                for lb in (i.status.load_balancer.ingress or [])
            ) if i.status.load_balancer else "<pending>"
            lines.append(f"{i.metadata.name:<24} {ingress_class:<11} {hosts:<35} {addr}")
        return "\n".join(lines)
    return _safe(_run)


@tool
def kubectl_get_pvc(namespace: str = "default") -> str:
    """List PersistentVolumeClaims — shows status, capacity, access modes, and storage class."""
    def _run():
        items = core_v1().list_namespaced_persistent_volume_claim(namespace).items
        if not items:
            return f"No PVCs in namespace '{namespace}'"
        lines = ["NAME                    STATUS   CAPACITY   ACCESS MODES   STORAGECLASS   AGE"]
        for pvc in items:
            capacity = (pvc.status.capacity or {}).get("storage", "?")
            access = ",".join(pvc.spec.access_modes or [])
            lines.append(
                f"{pvc.metadata.name:<24} {pvc.status.phase:<8} {capacity:<10} "
                f"{access:<14} {pvc.spec.storage_class_name or '<none>':<14} "
                f"{_age(pvc.metadata.creation_timestamp)}"
            )
        return "\n".join(lines)
    return _safe(_run)


@tool
def kubectl_get_resource_quotas(namespace: str = "default") -> str:
    """Show ResourceQuotas — used vs hard limits for CPU, memory, and object counts."""
    def _run():
        items = core_v1().list_namespaced_resource_quota(namespace).items
        if not items:
            return f"No resource quotas in namespace '{namespace}'"
        out = []
        for rq in items:
            out.append(f"ResourceQuota: {rq.metadata.name}")
            hard = rq.status.hard or {}
            used = rq.status.used or {}
            out.append(f"  {'RESOURCE':<40} {'USED':<15} HARD")
            for k in sorted(hard):
                out.append(f"  {k:<40} {used.get(k, '0'):<15} {hard[k]}")
        return "\n".join(out)
    return _safe(_run)


@tool
def get_cluster_summary(namespaces: str = "") -> str:
    """
    Get a high-level summary of cluster health.
    Pass a comma-separated list of namespaces, or leave empty to auto-discover
    all non-system namespaces.
    """
    def _run():
        # Resolve namespaces
        if namespaces:
            ns_list = [ns.strip() for ns in namespaces.split(",") if ns.strip()]
        else:
            all_ns = core_v1().list_namespace().items
            system_prefixes = ("kube-", "cert-manager", "monitoring", "istio")
            ns_list = [
                ns.metadata.name for ns in all_ns
                if not any(ns.metadata.name.startswith(p) for p in system_prefixes)
            ] or ["default"]

        sections = []

        # Nodes
        nodes = core_v1().list_node().items
        node_ready = sum(
            1 for n in nodes
            if any(c.type == "Ready" and c.status == "True" for c in (n.status.conditions or []))
        )
        sections.append(f"=== NODES: {node_ready}/{len(nodes)} Ready ===")

        for ns in ns_list:
            # Pods
            pods = core_v1().list_namespaced_pod(ns).items
            by_phase: dict[str, int] = {}
            crash_loops = []
            for p in pods:
                phase = p.status.phase or "Unknown"
                for cs in (p.status.container_statuses or []):
                    if cs.state and cs.state.waiting and cs.state.waiting.reason == "CrashLoopBackOff":
                        crash_loops.append(p.metadata.name)
                        phase = "CrashLoopBackOff"
                by_phase[phase] = by_phase.get(phase, 0) + 1
            phase_str = "  ".join(f"{k}:{v}" for k, v in sorted(by_phase.items()))
            sections.append(f"\n=== NAMESPACE: {ns} ===")
            sections.append(f"Pods: {phase_str or 'none'}")
            if crash_loops:
                sections.append(f"CrashLoopBackOff: {', '.join(crash_loops)}")

            # Deployments
            deps = apps_v1().list_namespaced_deployment(ns).items
            not_ready = [
                f"{d.metadata.name} ({d.status.ready_replicas or 0}/{d.spec.replicas})"
                for d in deps
                if (d.status.ready_replicas or 0) < (d.spec.replicas or 0)
            ]
            sections.append(f"Deployments: {len(deps)} total" + (
                f", NOT READY: {', '.join(not_ready)}" if not_ready else ", all ready"
            ))

            # Warning events
            warn_events = core_v1().list_namespaced_event(
                ns, field_selector="type=Warning"
            ).items
            if warn_events:
                sections.append(f"Warning Events: {len(warn_events)} (run kubectl_get_events for details)")

        return "\n".join(sections)
    return _safe(_run)


@tool
def kubectl_top_pods(namespace: str = "default") -> str:
    """Show CPU and memory usage for pods using the metrics-server."""
    def _run():
        if namespace == "--all-namespaces":
            result = custom_objects().list_cluster_custom_object(
                "metrics.k8s.io", "v1beta1", "pods"
            )
        else:
            result = custom_objects().list_namespaced_custom_object(
                "metrics.k8s.io", "v1beta1", namespace, "pods"
            )
        items = result.get("items", [])
        if not items:
            return f"No metrics available (is metrics-server installed?)"
        lines = ["NAME                                      CPU          MEMORY"]
        for pod in items:
            name = pod["metadata"]["name"]
            containers = pod.get("containers", [])
            total_cpu = sum(
                int(c["usage"]["cpu"].rstrip("n")) for c in containers
                if c["usage"]["cpu"].endswith("n")
            )
            total_mem_ki = sum(
                int(c["usage"]["memory"].rstrip("Ki")) for c in containers
                if c["usage"]["memory"].endswith("Ki")
            )
            lines.append(f"{name:<42} {total_cpu}n{'':5} {total_mem_ki}Ki")
        return "\n".join(lines)
    return _safe(_run)


@tool
def kubectl_top_nodes() -> str:
    """Show CPU and memory usage for all nodes using the metrics-server."""
    def _run():
        result = custom_objects().list_cluster_custom_object(
            "metrics.k8s.io", "v1beta1", "nodes"
        )
        items = result.get("items", [])
        if not items:
            return "No node metrics available (is metrics-server installed?)"
        lines = ["NAME                     CPU          MEMORY"]
        for node in items:
            name = node["metadata"]["name"]
            cpu = node["usage"]["cpu"]
            mem = node["usage"]["memory"]
            lines.append(f"{name:<25} {cpu:<13} {mem}")
        return "\n".join(lines)
    return _safe(_run)


@tool
def kubectl_get_configmaps(namespace: str = "default") -> str:
    """List all ConfigMaps in a namespace with their key count and age."""
    def _run():
        items = core_v1().list_namespaced_config_map(namespace).items
        if not items:
            return f"No ConfigMaps in namespace '{namespace}'"
        lines = ["NAME                              KEYS   AGE"]
        for cm in items:
            keys = len(cm.data or {}) + len(cm.binary_data or {})
            lines.append(f"{cm.metadata.name:<34} {keys:<6} {_age(cm.metadata.creation_timestamp)}")
        return "\n".join(lines)
    return _safe(_run)


@tool
def kubectl_get_configmap(configmap_name: str, namespace: str = "default") -> str:
    """Get the full contents of a specific ConfigMap including all data keys and values."""
    def _run():
        cm = core_v1().read_namespaced_config_map(configmap_name, namespace)
        out = [f"Name: {cm.metadata.name}", f"Namespace: {cm.metadata.namespace}",
               f"Created: {cm.metadata.creation_timestamp}"]
        if cm.metadata.labels:
            out.append(f"Labels: {cm.metadata.labels}")
        out.append("\nData:")
        for k, v in (cm.data or {}).items():
            preview = v[:200] + "..." if len(v) > 200 else v
            out.append(f"  {k}:\n    {preview.replace(chr(10), chr(10) + '    ')}")
        if cm.binary_data:
            out.append("\nBinaryData keys:")
            for k in cm.binary_data:
                out.append(f"  {k}: <binary>")
        return "\n".join(out)
    return _safe(_run)


@tool
def kubectl_get_statefulsets(namespace: str = "default") -> str:
    """List all StatefulSets — shows desired, ready replicas, and service name."""
    def _run():
        items = apps_v1().list_namespaced_stateful_set(namespace).items
        if not items:
            return f"No StatefulSets in namespace '{namespace}'"
        lines = ["NAME                          DESIRED   READY   SERVICE                  AGE"]
        for s in items:
            lines.append(
                f"{s.metadata.name:<30} {s.spec.replicas or 0:<9} "
                f"{s.status.ready_replicas or 0:<7} "
                f"{s.spec.service_name or '<none>':<25} "
                f"{_age(s.metadata.creation_timestamp)}"
            )
        return "\n".join(lines)
    return _safe(_run)


@tool
def kubectl_get_daemonsets(namespace: str = "default") -> str:
    """List all DaemonSets — shows desired, current, ready, and available counts."""
    def _run():
        items = apps_v1().list_namespaced_daemon_set(namespace).items
        if not items:
            return f"No DaemonSets in namespace '{namespace}'"
        lines = ["NAME                          DESIRED   CURRENT   READY   AVAILABLE   AGE"]
        for ds in items:
            s = ds.status
            lines.append(
                f"{ds.metadata.name:<30} {s.desired_number_scheduled or 0:<9} "
                f"{s.current_number_scheduled or 0:<9} {s.number_ready or 0:<7} "
                f"{s.number_available or 0:<11} {_age(ds.metadata.creation_timestamp)}"
            )
        return "\n".join(lines)
    return _safe(_run)


@tool
def kubectl_get_crds() -> str:
    """List all CustomResourceDefinitions installed in the cluster with their group/version/scope."""
    def _run():
        items = apiextensions_v1().list_custom_resource_definition().items
        if not items:
            return "No CRDs found in cluster"
        lines = ["NAME                                        GROUP                        VERSION   SCOPE       AGE"]
        for crd in sorted(items, key=lambda x: x.metadata.name):
            group = crd.spec.group
            versions = ",".join(v.name for v in (crd.spec.versions or []) if v.served)
            scope = crd.spec.scope
            lines.append(
                f"{crd.metadata.name:<44} {group:<28} {versions:<9} {scope:<11} "
                f"{_age(crd.metadata.creation_timestamp)}"
            )
        return "\n".join(lines)
    return _safe(_run)


@tool
def kubectl_get_custom_resources(
    group: str,
    version: str,
    plural: str,
    namespace: str = "",
) -> str:
    """
    List instances of a CustomResourceDefinition.
    group: e.g. 'cert-manager.io'
    version: e.g. 'v1'
    plural: the plural resource name, e.g. 'certificates'
    namespace: leave empty for cluster-scoped resources, or specify a namespace.
    Use kubectl_get_crds to discover available CRDs and their group/version/plural.
    """
    def _run():
        if namespace:
            result = custom_objects().list_namespaced_custom_object(
                group, version, namespace, plural
            )
        else:
            result = custom_objects().list_cluster_custom_object(group, version, plural)
        items = result.get("items", [])
        if not items:
            scope = f"namespace '{namespace}'" if namespace else "cluster"
            return f"No {plural}.{group}/{version} found in {scope}"
        lines = [f"NAME                                      NAMESPACE              AGE"]
        for item in items:
            meta = item.get("metadata", {})
            name = meta.get("name", "?")
            ns = meta.get("namespace", "<cluster>")
            created = meta.get("creationTimestamp")
            age_str = "?"
            if created:
                from datetime import datetime, timezone
                try:
                    ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    age_str = _age(ts)
                except Exception:
                    pass
            lines.append(f"{name:<42} {ns:<22} {age_str}")
        return "\n".join(lines)
    return _safe(_run)


@tool
def kubectl_rollout_history(
    resource_type: str,
    resource_name: str,
    namespace: str = "default",
) -> str:
    """
    Show rollout revision history for a deployment.
    Lists each revision, its ReplicaSet, and the change-cause annotation.
    resource_type must be 'deployment' (StatefulSet/DaemonSet history requires controller revisions).
    """
    def _run():
        if resource_type != "deployment":
            return (
                f"Rollout history for {resource_type} is not yet supported via the API. "
                f"Only 'deployment' is currently supported."
            )
        rss = apps_v1().list_namespaced_replica_set(namespace).items
        owned = []
        for rs in rss:
            for owner in (rs.metadata.owner_references or []):
                if owner.kind == "Deployment" and owner.name == resource_name:
                    annotations = rs.metadata.annotations or {}
                    rev = annotations.get("deployment.kubernetes.io/revision", "?")
                    cause = annotations.get("kubernetes.io/change-cause", "<none>")
                    images = ", ".join(
                        c.image for c in (rs.spec.template.spec.containers or [])
                    )
                    owned.append((int(rev) if rev.isdigit() else 0, rev, cause, images))
        if not owned:
            return f"No revision history found for deployment/{resource_name} in {namespace}"
        owned.sort()
        lines = ["REVISION   CHANGE-CAUSE                           IMAGE(S)"]
        for _, rev, cause, images in owned:
            lines.append(f"{rev:<10} {cause:<38} {images}")
        return "\n".join(lines)
    return _safe(_run)
