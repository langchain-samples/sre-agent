from .kubernetes_read import (
    kubectl_get_namespaces,
    kubectl_get_nodes,
    kubectl_get_pods,
    kubectl_describe_pod,
    kubectl_get_pod_logs,
    kubectl_get_deployments,
    kubectl_describe_deployment,
    kubectl_get_hpa,
    kubectl_get_events,
    kubectl_top_pods,
    kubectl_top_nodes,
    kubectl_get_services,
    kubectl_get_ingress,
    kubectl_get_pvc,
    kubectl_get_resource_quotas,
    get_cluster_summary,
    kubectl_get_configmaps,
    kubectl_get_configmap,
    kubectl_get_statefulsets,
    kubectl_get_daemonsets,
    kubectl_get_crds,
    kubectl_get_custom_resources,
    kubectl_rollout_history,
)
from .kubernetes_security import (
    kubectl_get_rbac_summary,
    kubectl_audit_pod_security,
    kubectl_get_network_policies,
    kubectl_audit_image_tags,
)
from .kubernetes_reliability import (
    kubectl_get_pdbs,
    kubectl_audit_probes,
    kubectl_get_endpoints,
    kubectl_audit_single_replicas,
)
from .kubernetes_batch import (
    kubectl_get_jobs,
    kubectl_get_cronjobs,
)
from .kubernetes_hygiene import (
    kubectl_audit_missing_limits,
    kubectl_get_pvs,
    kubectl_get_limit_ranges,
    kubectl_audit_selector_mismatch,
)
from .helm import (
    helm_list_releases,
    helm_get_release_values,
    helm_get_release_manifest,
    helm_search_chart_versions,
    helm_list_repos,
    helm_check_for_updates,
    helm_upgrade_release,
    helm_rollback_release,
    helm_release_history,
    helm_add_repo,
)
from .kubernetes_write import (
    kubectl_scale_deployment,
    kubectl_patch_resource_limits,
    kubectl_patch_hpa,
    kubectl_delete_pod,
    kubectl_apply_manifest,
    kubectl_cordon_node,
    kubectl_uncordon_node,
    kubectl_rollout_restart,
    kubectl_patch_configmap,
    kubectl_rollback_deployment,
    kubectl_apply_custom_resource,
    kubectl_delete_resource,
)

HELM_READ_TOOLS = [
    helm_list_releases,
    helm_get_release_values,
    helm_get_release_manifest,
    helm_search_chart_versions,
    helm_list_repos,
    helm_check_for_updates,
    helm_release_history,
]

HELM_WRITE_TOOLS = [
    helm_upgrade_release,
    helm_rollback_release,
    helm_add_repo,
]

READ_TOOLS = [
    kubectl_get_namespaces,
    kubectl_get_nodes,
    kubectl_get_pods,
    kubectl_describe_pod,
    kubectl_get_pod_logs,
    kubectl_get_deployments,
    kubectl_describe_deployment,
    kubectl_get_hpa,
    kubectl_get_events,
    kubectl_top_pods,
    kubectl_top_nodes,
    kubectl_get_services,
    kubectl_get_ingress,
    kubectl_get_pvc,
    kubectl_get_resource_quotas,
    get_cluster_summary,
    kubectl_get_configmaps,
    kubectl_get_configmap,
    kubectl_get_statefulsets,
    kubectl_get_daemonsets,
    kubectl_get_crds,
    kubectl_get_custom_resources,
    kubectl_rollout_history,
    # Security
    kubectl_get_rbac_summary,
    kubectl_audit_pod_security,
    kubectl_get_network_policies,
    kubectl_audit_image_tags,
    # Reliability
    kubectl_get_pdbs,
    kubectl_audit_probes,
    kubectl_get_endpoints,
    kubectl_audit_single_replicas,
    # Batch
    kubectl_get_jobs,
    kubectl_get_cronjobs,
    # Hygiene
    kubectl_audit_missing_limits,
    kubectl_get_pvs,
    kubectl_get_limit_ranges,
    kubectl_audit_selector_mismatch,
    *HELM_READ_TOOLS,
]

WRITE_TOOLS = [
    kubectl_scale_deployment,
    kubectl_patch_resource_limits,
    kubectl_patch_hpa,
    kubectl_delete_pod,
    kubectl_apply_manifest,
    kubectl_cordon_node,
    kubectl_uncordon_node,
    kubectl_rollout_restart,
    kubectl_patch_configmap,
    kubectl_rollback_deployment,
    kubectl_apply_custom_resource,
    kubectl_delete_resource,
    *HELM_WRITE_TOOLS,
]

WRITE_TOOL_NAMES = {tool.name: True for tool in WRITE_TOOLS}
