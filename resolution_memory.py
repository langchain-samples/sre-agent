"""Pure functions correlating approved write-tool calls to resolved findings.

The monitoring loop (``finding_state``) and the HITL approval log
(``hitl_audit``) are otherwise unconnected: nothing links "this finding was
open" to "this fix was approved" to "the finding later resolved." This module
is the matching logic for that link — given a resolved finding's identity and
a time-ordered list of approved decisions, decide whether one of them plausibly
caused the resolution. Persistence (the ``resolution_memory`` table) and the
correlation loop that calls this live in ``persistence.py`` / ``scheduler.py``;
everything here is offline and unit-testable, mirroring ``monitor_state.py``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from monitor_state import normalize_resource_name


@dataclass(frozen=True)
class Target:
    """The single resource a write-tool call acted on."""

    namespace: Optional[str]  # None for cluster-scoped tools (nodes)
    kind: str                 # "" when the tool can't tell us (custom resources)
    name: str                 # normalized/lowercased, comparable to a fingerprint


# tool_name -> (namespace_arg, name_arg, kind). Only tools that name exactly one
# resource are covered — bulk tools (targets_json) and kubectl_apply_manifest
# (raw YAML) have no clean single name to extract and are deliberately absent,
# so extract_target() returns None for them.
_TOOL_TARGETS: dict[str, tuple[Optional[str], str, str]] = {
    "kubectl_scale_deployment": ("namespace", "deployment_name", "deployment"),
    "kubectl_patch_resource_limits": ("namespace", "deployment_name", "deployment"),
    "kubectl_patch_hpa": ("namespace", "hpa_name", "hpa"),
    "kubectl_delete_pod": ("namespace", "pod_name", "pod"),
    "kubectl_rollback_deployment": ("namespace", "deployment_name", "deployment"),
    "kubectl_resize_pvc": ("namespace", "pvc_name", "pvc"),
    "kubectl_cordon_node": (None, "node_name", "node"),
    "kubectl_uncordon_node": (None, "node_name", "node"),
}


def extract_target(tool_name: str, tool_args: dict) -> Optional[Target]:
    """The resource a write-tool call acted on, or ``None`` if not correlatable.

    Reuses ``monitor_state.normalize_resource_name`` so a raw pod name (e.g.
    ``api-7f9c8d``, as passed to ``kubectl_delete_pod``) compares correctly
    against a fingerprint's already-normalized, suffix-stripped name.
    """
    tool_args = tool_args or {}

    if tool_name == "kubectl_rollout_restart":
        name = str(tool_args.get("resource_name") or "")
        kind = str(tool_args.get("resource_type") or "").strip().lower()
        if not (name and kind):
            return None
        ns = str(tool_args.get("namespace") or "")
        return Target(ns, kind, normalize_resource_name(kind, name).lower())

    if tool_name == "kubectl_delete_custom_resource":
        name = str(tool_args.get("name") or "")
        if not name:
            return None
        ns = str(tool_args.get("namespace") or "") or None
        # Kind is whatever the CRD's kind is, not derivable from group/plural
        # without a live cluster lookup — match on namespace+name only.
        return Target(ns, "", name.lower())

    spec = _TOOL_TARGETS.get(tool_name)
    if not spec:
        return None
    ns_field, name_field, kind = spec
    name = str(tool_args.get(name_field) or "")
    if not name:
        return None
    ns = str(tool_args.get(ns_field) or "") if ns_field else None
    return Target(ns, kind, normalize_resource_name(kind, name).lower())


def _matches(target: Target, finding_ns: str, finding_kind: str, finding_name: str) -> bool:
    if target.namespace is not None and target.namespace.strip().lower() != finding_ns.strip().lower():
        return False
    if target.kind and target.kind != finding_kind.lower():
        return False
    return target.name == finding_name.lower()


def find_best_match(
    finding_ns: str, finding_kind: str, finding_name: str, decisions: list[dict]
) -> Optional[dict]:
    """The most recent approved decision that plausibly targeted this finding.

    ``decisions`` must be most-recent-first. When more than one distinct
    approved change touched the same resource before it resolved, the most
    recent one wins — a deliberate simplifying assumption (the last thing done
    before it got better is the best available guess), not a guarantee.
    """
    for decision in decisions:
        target = extract_target(decision.get("tool_name", ""), decision.get("tool_args") or {})
        if target and _matches(target, finding_ns, finding_kind, finding_name):
            return decision
    return None
