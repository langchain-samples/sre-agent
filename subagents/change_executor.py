"""Change Executor subagent — applies approved changes with HITL for every write operation."""
from tools import (
    # Read tools for verification
    kubectl_get_pods,
    kubectl_get_deployments,
    kubectl_get_hpa,
    kubectl_top_pods,
    kubectl_describe_deployment,
    # Write tools — ALL require HITL approval
    kubectl_scale_deployment,
    kubectl_patch_resource_limits,
    kubectl_patch_hpa,
    kubectl_delete_pod,
    kubectl_apply_manifest,
    kubectl_cordon_node,
    kubectl_uncordon_node,
    kubectl_rollout_restart,
)

# All write tool names require human approval
CHANGE_EXECUTOR_INTERRUPT_ON = {
    "kubectl_scale_deployment": True,
    "kubectl_patch_resource_limits": True,
    "kubectl_patch_hpa": True,
    "kubectl_delete_pod": True,
    "kubectl_apply_manifest": True,
    "kubectl_cordon_node": True,
    "kubectl_uncordon_node": True,
    "kubectl_rollout_restart": True,
}

change_executor_subagent = {
    "name": "change-executor",
    "description": (
        "Execute approved Kubernetes changes: scaling deployments, updating resource "
        "limits, patching HPAs, restarting workloads, and applying manifests. "
        "Every write operation requires explicit human approval before execution. "
        "Verifies state before and after each change."
    ),
    "system_prompt": (
        "You are a Kubernetes change execution specialist. You apply changes safely.\n"
        "For EVERY change you execute:\n"
        "1. BEFORE: read current state (describe the resource to be modified)\n"
        "2. EXECUTE: apply the change (this will pause for human approval)\n"
        "3. AFTER: verify the change was applied correctly\n"
        "4. REPORT: summarize what was done and the new state\n\n"
        "Guidelines:\n"
        "- Make one change at a time and verify before proceeding to the next\n"
        "- For scaling: check pod readiness after scaling up\n"
        "- For resource limits: describe the deployment after patching\n"
        "- For pod deletes: verify the new pod starts healthy\n"
        "- If a change fails, stop and report the error — do not retry automatically\n"
        "- Use rollout_restart instead of delete for graceful workload restarts\n"
        "Always include the before/after state in your final report."
    ),
    "tools": [
        # Read tools for pre/post verification
        kubectl_get_pods,
        kubectl_get_deployments,
        kubectl_get_hpa,
        kubectl_top_pods,
        kubectl_describe_deployment,
        # Write tools (all require HITL)
        kubectl_scale_deployment,
        kubectl_patch_resource_limits,
        kubectl_patch_hpa,
        kubectl_delete_pod,
        kubectl_apply_manifest,
        kubectl_cordon_node,
        kubectl_uncordon_node,
        kubectl_rollout_restart,
    ],
    "interrupt_on": CHANGE_EXECUTOR_INTERRUPT_ON,
}
