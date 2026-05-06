"""Write Kubernetes tools — ALL require human approval via HITL before execution."""
from __future__ import annotations
import json
import traceback
import yaml
from langchain.tools import tool
from kubernetes.client.rest import ApiException
from kubernetes import client as k8s_client
from .k8s_client import core_v1, apps_v1, autoscaling_v2, networking_v1, custom_objects, apiextensions_v1


def _safe(fn):
    try:
        return fn()
    except ApiException as e:
        return f"ERROR [{e.status}]: {e.reason} — {e.body}"
    except Exception:
        return f"ERROR: {traceback.format_exc(limit=3)}"


@tool
def kubectl_scale_deployment(
    deployment_name: str,
    namespace: str,
    replicas: int,
) -> str:
    """
    Scale a deployment to the specified number of replicas.
    REQUIRES HUMAN APPROVAL before execution.
    """
    def _run():
        patch = {"spec": {"replicas": replicas}}
        apps_v1().patch_namespaced_deployment_scale(
            deployment_name, namespace, patch
        )
        return f"Scaled {deployment_name} in {namespace} to {replicas} replicas."
    return _safe(_run)


@tool
def kubectl_patch_resource_limits(
    deployment_name: str,
    namespace: str,
    container_name: str,
    cpu_request: str = "",
    cpu_limit: str = "",
    memory_request: str = "",
    memory_limit: str = "",
) -> str:
    """
    Patch CPU and/or memory resource requests/limits for a container in a deployment.
    Provide values in Kubernetes format (e.g. cpu_limit='500m', memory_limit='512Mi').
    REQUIRES HUMAN APPROVAL before execution.
    """
    def _run():
        resources: dict = {"requests": {}, "limits": {}}
        if cpu_request:
            resources["requests"]["cpu"] = cpu_request
        if memory_request:
            resources["requests"]["memory"] = memory_request
        if cpu_limit:
            resources["limits"]["cpu"] = cpu_limit
        if memory_limit:
            resources["limits"]["memory"] = memory_limit
        resources = {k: v for k, v in resources.items() if v}
        if not resources:
            return "ERROR: at least one resource value must be provided"

        patch = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {"name": container_name, "resources": resources}
                        ]
                    }
                }
            }
        }
        apps_v1().patch_namespaced_deployment(deployment_name, namespace, patch)
        return f"Patched resources on {container_name} in {deployment_name}/{namespace}: {resources}"
    return _safe(_run)


@tool
def kubectl_patch_hpa(
    hpa_name: str,
    namespace: str,
    min_replicas: int = 0,
    max_replicas: int = 0,
    target_cpu_utilization: int = 0,
) -> str:
    """
    Patch a HorizontalPodAutoscaler — update min/max replicas or target CPU %.
    Pass 0 to leave a value unchanged.
    REQUIRES HUMAN APPROVAL before execution.
    """
    def _run():
        patch: dict = {"spec": {}}
        if min_replicas > 0:
            patch["spec"]["minReplicas"] = min_replicas
        if max_replicas > 0:
            patch["spec"]["maxReplicas"] = max_replicas
        if target_cpu_utilization > 0:
            patch["spec"]["metrics"] = [{
                "type": "Resource",
                "resource": {
                    "name": "cpu",
                    "target": {
                        "type": "Utilization",
                        "averageUtilization": target_cpu_utilization,
                    },
                },
            }]
        if not patch["spec"]:
            return "ERROR: at least one HPA field must be specified"
        autoscaling_v2().patch_namespaced_horizontal_pod_autoscaler(hpa_name, namespace, patch)
        return f"Patched HPA {hpa_name}/{namespace}: {patch['spec']}"
    return _safe(_run)


@tool
def kubectl_delete_pod(pod_name: str, namespace: str, force: bool = False) -> str:
    """
    Delete a pod to force a restart (the controller will recreate it).
    Use force=True only for pods stuck in Terminating.
    REQUIRES HUMAN APPROVAL before execution.
    """
    def _run():
        body = k8s_client.V1DeleteOptions()
        if force:
            body.grace_period_seconds = 0
        core_v1().delete_namespaced_pod(pod_name, namespace, body=body)
        return f"Deleted pod {pod_name} in {namespace}."
    return _safe(_run)


@tool
def kubectl_apply_manifest(manifest_yaml: str) -> str:
    """
    Apply a Kubernetes manifest (YAML string) to the cluster — create or update.
    Supports: Deployment, StatefulSet, DaemonSet, Service, ConfigMap, Ingress,
    HorizontalPodAutoscaler, and most standard resource types.
    For CRDs/custom resources use kubectl_apply_custom_resource instead.
    REQUIRES HUMAN APPROVAL before execution.
    """
    def _run():
        doc = yaml.safe_load(manifest_yaml)
        kind = doc.get("kind", "")
        name = doc.get("metadata", {}).get("name", "?")
        namespace = doc.get("metadata", {}).get("namespace", "default")

        def _create_or_patch(create_fn, patch_fn, *patch_args):
            try:
                create_fn(namespace, doc)
                return f"Created {kind} {name} in {namespace}"
            except ApiException as e:
                if e.status == 409:
                    patch_fn(*patch_args, namespace, doc)
                    return f"Updated {kind} {name} in {namespace}"
                raise

        if kind == "Deployment":
            return _create_or_patch(
                apps_v1().create_namespaced_deployment,
                apps_v1().patch_namespaced_deployment, name
            )
        elif kind == "StatefulSet":
            return _create_or_patch(
                apps_v1().create_namespaced_stateful_set,
                apps_v1().patch_namespaced_stateful_set, name
            )
        elif kind == "DaemonSet":
            return _create_or_patch(
                apps_v1().create_namespaced_daemon_set,
                apps_v1().patch_namespaced_daemon_set, name
            )
        elif kind == "ConfigMap":
            return _create_or_patch(
                core_v1().create_namespaced_config_map,
                core_v1().patch_namespaced_config_map, name
            )
        elif kind == "Service":
            return _create_or_patch(
                core_v1().create_namespaced_service,
                core_v1().patch_namespaced_service, name
            )
        elif kind == "Ingress":
            return _create_or_patch(
                networking_v1().create_namespaced_ingress,
                networking_v1().patch_namespaced_ingress, name
            )
        elif kind == "HorizontalPodAutoscaler":
            return _create_or_patch(
                autoscaling_v2().create_namespaced_horizontal_pod_autoscaler,
                autoscaling_v2().patch_namespaced_horizontal_pod_autoscaler, name
            )
        else:
            return (
                f"ERROR: kubectl_apply_manifest does not support kind={kind}. "
                f"For custom resources use kubectl_apply_custom_resource."
            )
    return _safe(_run)


@tool
def kubectl_cordon_node(node_name: str) -> str:
    """
    Cordon a node to prevent new pods from being scheduled on it.
    REQUIRES HUMAN APPROVAL before execution.
    """
    def _run():
        patch = {"spec": {"unschedulable": True}}
        core_v1().patch_node(node_name, patch)
        return f"Cordoned node {node_name}."
    return _safe(_run)


@tool
def kubectl_uncordon_node(node_name: str) -> str:
    """
    Uncordon a node to allow scheduling again.
    REQUIRES HUMAN APPROVAL before execution.
    """
    def _run():
        patch = {"spec": {"unschedulable": False}}
        core_v1().patch_node(node_name, patch)
        return f"Uncordoned node {node_name}."
    return _safe(_run)


@tool
def kubectl_rollout_restart(
    resource_type: str,
    resource_name: str,
    namespace: str,
) -> str:
    """
    Trigger a rolling restart of a workload (deployment, statefulset, or daemonset).
    This is the safest way to restart all pods in a workload.
    resource_type must be: 'deployment', 'statefulset', or 'daemonset'
    REQUIRES HUMAN APPROVAL before execution.
    """
    def _run():
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        patch = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {"kubectl.kubernetes.io/restartedAt": now}
                    }
                }
            }
        }
        if resource_type == "deployment":
            apps_v1().patch_namespaced_deployment(resource_name, namespace, patch)
        elif resource_type == "statefulset":
            apps_v1().patch_namespaced_stateful_set(resource_name, namespace, patch)
        elif resource_type == "daemonset":
            apps_v1().patch_namespaced_daemon_set(resource_name, namespace, patch)
        else:
            return f"ERROR: resource_type must be deployment, statefulset, or daemonset"
        return f"Rolling restart triggered for {resource_type}/{resource_name} in {namespace}."
    return _safe(_run)


@tool
def kubectl_patch_configmap(
    configmap_name: str,
    namespace: str,
    data_json: str,
) -> str:
    """
    Patch specific keys in a ConfigMap's data section.
    data_json: JSON object mapping key names to new values. Unmentioned keys are unchanged.
    Example: data_json='{"log_level": "debug", "timeout_seconds": "30"}'
    REQUIRES HUMAN APPROVAL before execution.
    """
    def _run():
        try:
            data_dict = json.loads(data_json)
        except json.JSONDecodeError as e:
            return f"ERROR: Invalid JSON in data_json: {e}"
        if not isinstance(data_dict, dict):
            return "ERROR: data_json must be a JSON object (dict)"
        patch = {"data": data_dict}
        core_v1().patch_namespaced_config_map(configmap_name, namespace, patch)
        return f"Patched ConfigMap {configmap_name}/{namespace}: updated keys {list(data_dict.keys())}"
    return _safe(_run)


@tool
def kubectl_rollback_deployment(
    deployment_name: str,
    namespace: str,
    revision: int = 0,
) -> str:
    """
    Roll back a deployment to a previous revision.
    revision=0 (default) rolls back to the immediately previous revision.
    Use kubectl_rollout_history to see available revisions.
    REQUIRES HUMAN APPROVAL before execution.
    """
    def _run():
        deploy = apps_v1().read_namespaced_deployment(deployment_name, namespace)
        current_rev = int(
            (deploy.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "0")
        )
        target_rev = revision if revision > 0 else (current_rev - 1)
        if target_rev <= 0:
            return f"ERROR: Cannot determine target revision (current={current_rev})"

        rss = apps_v1().list_namespaced_replica_set(namespace).items
        target_rs = None
        for rs in rss:
            for owner in (rs.metadata.owner_references or []):
                if owner.kind == "Deployment" and owner.name == deployment_name:
                    rev = (rs.metadata.annotations or {}).get(
                        "deployment.kubernetes.io/revision", ""
                    )
                    if rev == str(target_rev):
                        target_rs = rs
                        break
            if target_rs:
                break

        if target_rs is None:
            return (
                f"ERROR: No ReplicaSet found for revision {target_rev} of "
                f"{deployment_name}/{namespace}. It may have been garbage-collected."
            )

        patch = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {"name": c.name, "image": c.image}
                            for c in (target_rs.spec.template.spec.containers or [])
                        ]
                    }
                }
            }
        }
        apps_v1().patch_namespaced_deployment(deployment_name, namespace, patch)
        images = ", ".join(
            f"{c.name}={c.image}" for c in (target_rs.spec.template.spec.containers or [])
        )
        return (
            f"Rolled back {deployment_name}/{namespace} to revision {target_rev}. "
            f"Images: {images}"
        )
    return _safe(_run)


@tool
def kubectl_apply_custom_resource(
    group: str,
    version: str,
    plural: str,
    name: str,
    body_yaml: str,
    namespace: str = "",
) -> str:
    """
    Create or update a CustomResource instance.
    group: API group, e.g. 'cert-manager.io'
    version: API version, e.g. 'v1'
    plural: plural resource name, e.g. 'certificates'
    name: resource name
    body_yaml: full YAML manifest for the custom resource
    namespace: leave empty for cluster-scoped CRDs
    Use kubectl_get_crds to discover available CRDs.
    REQUIRES HUMAN APPROVAL before execution.
    """
    def _run():
        body = yaml.safe_load(body_yaml)
        if namespace:
            try:
                custom_objects().create_namespaced_custom_object(
                    group, version, namespace, plural, body
                )
                return f"Created {plural}.{group}/{version} '{name}' in {namespace}"
            except ApiException as e:
                if e.status == 409:
                    custom_objects().patch_namespaced_custom_object(
                        group, version, namespace, plural, name, body
                    )
                    return f"Updated {plural}.{group}/{version} '{name}' in {namespace}"
                raise
        else:
            try:
                custom_objects().create_cluster_custom_object(group, version, plural, body)
                return f"Created cluster-scoped {plural}.{group}/{version} '{name}'"
            except ApiException as e:
                if e.status == 409:
                    custom_objects().patch_cluster_custom_object(
                        group, version, plural, name, body
                    )
                    return f"Updated cluster-scoped {plural}.{group}/{version} '{name}'"
                raise
    return _safe(_run)


@tool
def kubectl_delete_resource(
    resource_type: str,
    resource_name: str,
    namespace: str = "default",
) -> str:
    """
    Delete a Kubernetes resource. Supported resource_type values:
    deployment, statefulset, daemonset, service, configmap, ingress, hpa,
    replicaset, job, cronjob.
    Use kubectl_delete_pod to delete pods.
    REQUIRES HUMAN APPROVAL before execution.
    """
    def _run():
        rt = resource_type.lower()
        body = k8s_client.V1DeleteOptions()
        if rt == "deployment":
            apps_v1().delete_namespaced_deployment(resource_name, namespace, body=body)
        elif rt == "statefulset":
            apps_v1().delete_namespaced_stateful_set(resource_name, namespace, body=body)
        elif rt == "daemonset":
            apps_v1().delete_namespaced_daemon_set(resource_name, namespace, body=body)
        elif rt == "service":
            core_v1().delete_namespaced_service(resource_name, namespace, body=body)
        elif rt == "configmap":
            core_v1().delete_namespaced_config_map(resource_name, namespace, body=body)
        elif rt == "ingress":
            networking_v1().delete_namespaced_ingress(resource_name, namespace, body=body)
        elif rt == "hpa":
            autoscaling_v2().delete_namespaced_horizontal_pod_autoscaler(
                resource_name, namespace, body=body
            )
        elif rt == "replicaset":
            apps_v1().delete_namespaced_replica_set(resource_name, namespace, body=body)
        else:
            return (
                f"ERROR: unsupported resource_type '{resource_type}'. "
                f"Supported: deployment, statefulset, daemonset, service, configmap, "
                f"ingress, hpa, replicaset."
            )
        return f"Deleted {resource_type}/{resource_name} in {namespace}."
    return _safe(_run)


@tool
def kubectl_scale_bulk(targets_json: str) -> str:
    """
    Scale multiple deployments or statefulsets in one operation.
    targets_json: JSON array of objects with keys:
      - deployment_name (str)
      - namespace (str)
      - replicas (int)
      - kind (str, optional): "deployment" (default) or "statefulset"
    Example:
      '[{"deployment_name":"web","namespace":"prod","replicas":0},
        {"deployment_name":"api","namespace":"prod","replicas":0,"kind":"statefulset"}]'
    REQUIRES HUMAN APPROVAL before execution.
    """
    def _run():
        try:
            targets = json.loads(targets_json)
        except json.JSONDecodeError as e:
            return f"ERROR: Invalid JSON — {e}"
        results = []
        for t in targets:
            name = t.get("deployment_name") or t.get("name", "")
            ns = t.get("namespace", "default")
            replicas = t.get("replicas", 0)
            kind = t.get("kind", "deployment").lower()
            try:
                patch = {"spec": {"replicas": replicas}}
                if kind == "statefulset":
                    apps_v1().patch_namespaced_stateful_set(name, ns, patch)
                else:
                    apps_v1().patch_namespaced_deployment_scale(name, ns, patch)
                results.append(f"✓ {kind}/{name} in {ns} → {replicas} replicas")
            except ApiException as e:
                results.append(f"✗ {kind}/{name} in {ns}: [{e.status}] {e.reason}")
        return "\n".join(results) if results else "No targets provided."
    return _safe(_run)


@tool
def kubectl_delete_resources_bulk(targets_json: str) -> str:
    """
    Delete multiple Kubernetes resources in one operation.
    targets_json: JSON array of objects with keys:
      - resource_type (str): deployment, statefulset, daemonset, service, configmap,
                             ingress, hpa, pvc, pod
      - resource_name (str)
      - namespace (str, default "default")
    Example:
      '[{"resource_type":"deployment","resource_name":"web","namespace":"prod"},
        {"resource_type":"pvc","resource_name":"data-0","namespace":"prod"}]'
    REQUIRES HUMAN APPROVAL before execution.
    """
    def _run():
        try:
            targets = json.loads(targets_json)
        except json.JSONDecodeError as e:
            return f"ERROR: Invalid JSON — {e}"
        body = k8s_client.V1DeleteOptions()
        results = []
        for t in targets:
            rt = t.get("resource_type", "").lower()
            name = t.get("resource_name", "")
            ns = t.get("namespace", "default")
            try:
                if rt == "deployment":
                    apps_v1().delete_namespaced_deployment(name, ns, body=body)
                elif rt == "statefulset":
                    apps_v1().delete_namespaced_stateful_set(name, ns, body=body)
                elif rt == "daemonset":
                    apps_v1().delete_namespaced_daemon_set(name, ns, body=body)
                elif rt == "service":
                    core_v1().delete_namespaced_service(name, ns, body=body)
                elif rt == "configmap":
                    core_v1().delete_namespaced_config_map(name, ns, body=body)
                elif rt == "ingress":
                    networking_v1().delete_namespaced_ingress(name, ns, body=body)
                elif rt == "hpa":
                    autoscaling_v2().delete_namespaced_horizontal_pod_autoscaler(name, ns, body=body)
                elif rt == "pvc":
                    core_v1().delete_namespaced_persistent_volume_claim(name, ns, body=body)
                elif rt == "pod":
                    core_v1().delete_namespaced_pod(name, ns, body=body)
                else:
                    results.append(f"✗ unsupported resource_type '{rt}'")
                    continue
                results.append(f"✓ deleted {rt}/{name} in {ns}")
            except ApiException as e:
                results.append(f"✗ {rt}/{name} in {ns}: [{e.status}] {e.reason}")
        return "\n".join(results) if results else "No targets provided."
    return _safe(_run)
