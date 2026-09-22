"""Tests for the pure fix-to-finding correlation logic in resolution_memory.py."""
from __future__ import annotations

from resolution_memory import Target, extract_target, find_best_match


def test_extract_target_scale_deployment():
    target = extract_target(
        "kubectl_scale_deployment",
        {"deployment_name": "api", "namespace": "prod", "replicas": 5},
    )
    assert target == Target("prod", "deployment", "api")


def test_extract_target_patch_resource_limits():
    target = extract_target(
        "kubectl_patch_resource_limits",
        {"deployment_name": "api", "namespace": "prod", "container_name": "app", "memory_limit": "512Mi"},
    )
    assert target == Target("prod", "deployment", "api")


def test_extract_target_patch_hpa():
    target = extract_target(
        "kubectl_patch_hpa", {"hpa_name": "api-hpa", "namespace": "prod", "max_replicas": 10}
    )
    assert target == Target("prod", "hpa", "api-hpa")


def test_extract_target_delete_pod_normalizes_the_random_suffix():
    target = extract_target(
        "kubectl_delete_pod", {"pod_name": "api-6b474476c4-6nqxr", "namespace": "prod"}
    )
    assert target == Target("prod", "pod", "api")


def test_extract_target_rollback_deployment():
    target = extract_target(
        "kubectl_rollback_deployment", {"deployment_name": "api", "namespace": "prod", "revision": 3}
    )
    assert target == Target("prod", "deployment", "api")


def test_extract_target_resize_pvc():
    target = extract_target(
        "kubectl_resize_pvc", {"pvc_name": "data", "namespace": "prod", "new_size": "20Gi"}
    )
    assert target == Target("prod", "pvc", "data")


def test_extract_target_cordon_and_uncordon_node_are_cluster_scoped():
    assert extract_target("kubectl_cordon_node", {"node_name": "ip-10-0-1-2"}) == Target(
        None, "node", "ip-10-0-1-2"
    )
    assert extract_target("kubectl_uncordon_node", {"node_name": "ip-10-0-1-2"}) == Target(
        None, "node", "ip-10-0-1-2"
    )


def test_extract_target_rollout_restart_uses_its_own_resource_type():
    target = extract_target(
        "kubectl_rollout_restart",
        {"resource_type": "StatefulSet", "resource_name": "web", "namespace": "prod"},
    )
    assert target == Target("prod", "statefulset", "web")


def test_extract_target_delete_custom_resource_has_no_kind():
    target = extract_target(
        "kubectl_delete_custom_resource",
        {"group": "apps.langchain.ai", "version": "v1", "plural": "lgps", "name": "my-lgp", "namespace": "prod"},
    )
    assert target == Target("prod", "", "my-lgp")


def test_extract_target_returns_none_for_bulk_and_manifest_tools():
    assert extract_target("kubectl_scale_bulk", {"targets_json": "[]"}) is None
    assert extract_target("kubectl_delete_resources_bulk", {"targets_json": "[]"}) is None
    assert extract_target("kubectl_apply_manifest", {"manifest_yaml": "..."}) is None


def test_extract_target_returns_none_for_unknown_tool():
    assert extract_target("some_read_only_tool", {}) is None


def test_extract_target_returns_none_when_the_name_arg_is_missing():
    assert extract_target("kubectl_scale_deployment", {"namespace": "prod"}) is None


def test_find_best_match_returns_the_matching_decision():
    decisions = [
        {"tool_name": "kubectl_scale_deployment", "tool_args": {"deployment_name": "api", "namespace": "prod", "replicas": 5}, "actor": "eric"},
    ]
    match = find_best_match("prod", "deployment", "api", decisions)
    assert match is not None
    assert match["actor"] == "eric"


def test_find_best_match_ignores_a_decision_for_a_different_resource():
    decisions = [
        {"tool_name": "kubectl_scale_deployment", "tool_args": {"deployment_name": "worker", "namespace": "prod", "replicas": 5}},
    ]
    assert find_best_match("prod", "deployment", "api", decisions) is None


def test_find_best_match_returns_none_with_no_decisions():
    assert find_best_match("prod", "deployment", "api", []) is None


def test_find_best_match_most_recent_of_several_candidates_wins():
    # Newest-first, as the caller (persistence.decisions_since) returns them.
    decisions = [
        {"tool_name": "kubectl_rollout_restart", "tool_args": {"resource_type": "deployment", "resource_name": "api", "namespace": "prod"}, "actor": "later"},
        {"tool_name": "kubectl_scale_deployment", "tool_args": {"deployment_name": "api", "namespace": "prod", "replicas": 5}, "actor": "earlier"},
    ]
    match = find_best_match("prod", "deployment", "api", decisions)
    assert match["actor"] == "later"


def test_find_best_match_respects_namespace():
    decisions = [
        {"tool_name": "kubectl_scale_deployment", "tool_args": {"deployment_name": "api", "namespace": "staging", "replicas": 5}},
    ]
    assert find_best_match("prod", "deployment", "api", decisions) is None


def test_find_best_match_ignores_kind_mismatch_for_custom_resources():
    # Custom-resource deletes carry no derivable kind, so they should still
    # match on namespace+name alone against a finding of any kind.
    decisions = [
        {"tool_name": "kubectl_delete_custom_resource", "tool_args": {"group": "g", "version": "v1", "plural": "p", "name": "my-lgp", "namespace": "prod"}},
    ]
    assert find_best_match("prod", "lgp", "my-lgp", decisions) is not None
