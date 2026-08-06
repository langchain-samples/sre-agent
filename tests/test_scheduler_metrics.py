"""Unit tests for the scheduled collector's metrics wiring and snapshot format.

No cluster required. These cover the failure modes that matter for a collector
that must never break the scheduled check: metrics-server absent, unusual units,
and exotic HPA metric shapes.
"""
from __future__ import annotations

import re

import pytest

from scheduler import _format_hpa_metrics, _format_snapshot, _metric_value


def base_data(**over):
    data = {
        "nodes": [{"name": "ip-10-0-1-1", "status": "Ready", "version": "v1.30"}],
        "pods": [{"namespace": "prod", "name": "api-1", "status": "Running",
                  "restarts": 0, "age": "3d"}],
        "unhealthy_pods": [],
        "events": [],
        "hpas": [],
        "deployments": [{"namespace": "prod", "name": "api", "desired": 2,
                         "ready": 2, "available": 2}],
        "node_metrics": [],
        "pod_metrics": {},
        "errors": [],
    }
    data.update(over)
    return data


# ---------------------------------------------------------------------------
# Snapshot degradation
# ---------------------------------------------------------------------------

def test_snapshot_without_metrics_omits_the_utilization_section():
    """metrics-server absent must produce the same output as before this change."""
    out = _format_snapshot(base_data())
    assert "NODE UTILIZATION" not in out
    assert "=== NODES ===" in out
    assert "=== DEPLOYMENTS ===" in out


def test_snapshot_renders_node_utilization_when_present():
    out = _format_snapshot(base_data(node_metrics=[
        {"name": "ip-10-0-1-1", "cpu": "142m", "memory": "1802140Ki"},
    ]))
    assert "=== NODE UTILIZATION ===" in out
    assert "ip-10-0-1-1" in out
    # Normalized for readability. base_data's node carries no allocatable, so
    # there is no percentage to show.
    assert "cpu=142m" in out
    assert "memory=1.7Gi" in out


def test_metrics_failure_surfaces_as_a_collection_error_not_a_crash():
    out = _format_snapshot(base_data(errors=["node metrics: 503 Service Unavailable"]))
    assert "=== COLLECTION ERRORS ===" in out
    assert "node metrics" in out
    assert "NODE UTILIZATION" not in out


# ---------------------------------------------------------------------------
# HPA metric rendering
# ---------------------------------------------------------------------------

class Target:
    def __init__(self, average_utilization=None, average_value=None, value=None):
        self.average_utilization = average_utilization
        self.average_value = average_value
        self.value = value


class ResourceBlock:
    def __init__(self, name, current=None, target=None):
        self.name = name
        self.current = current
        self.target = target


class NamedMetric:
    def __init__(self, name):
        self.name = name


class ExternalBlock:
    def __init__(self, name, current=None, target=None):
        self.metric = NamedMetric(name)
        self.current = current
        self.target = target


class Entry:
    """Stand-in for a V2MetricSpec / V2MetricStatus."""
    def __init__(self, mtype, block):
        self.type = mtype
        setattr(self, mtype.lower(), block)


def test_hpa_line_shows_current_versus_target_utilization():
    hpa = {
        "current_metrics": [Entry("Resource", ResourceBlock("cpu", current=Target(average_utilization=12)))],
        "target_metrics": [Entry("Resource", ResourceBlock("cpu", target=Target(average_utilization=80)))],
    }
    assert _format_hpa_metrics(hpa) == " (cpu 12%/target 80%)"


def test_hpa_metrics_is_empty_when_nothing_is_available():
    assert _format_hpa_metrics({}) == ""
    assert _format_hpa_metrics({"current_metrics": [], "target_metrics": []}) == ""


def test_hpa_target_without_current_still_renders():
    hpa = {"target_metrics": [Entry("Resource", ResourceBlock("cpu", target=Target(average_utilization=80)))]}
    assert _format_hpa_metrics(hpa) == " (cpu ?/target 80%)"


def test_external_metric_shape_is_tolerated():
    hpa = {
        "current_metrics": [Entry("External", ExternalBlock("sqs_depth", current=Target(average_value="41")))],
        "target_metrics": [Entry("External", ExternalBlock("sqs_depth", target=Target(average_value="30")))],
    }
    assert _format_hpa_metrics(hpa) == " (sqs_depth 41/target 30)"


def test_unknown_metric_shape_is_skipped_rather_than_raising():
    class Weird:
        type = "Galactic"
    assert _metric_value(Weird(), current=True) is None
    assert _format_hpa_metrics({"current_metrics": [Weird()]}) == ""


def test_malformed_entry_does_not_raise():
    assert _metric_value(None, current=True) is None
    assert _metric_value(object(), current=False) is None


def test_hpa_line_in_snapshot_includes_metrics_and_at_max_flag():
    data = base_data(hpas=[{
        "namespace": "prod", "name": "api", "min": 1, "max": 1,
        "current": 1, "desired": 1,
        "current_metrics": [Entry("Resource", ResourceBlock("cpu", current=Target(average_utilization=12)))],
        "target_metrics": [Entry("Resource", ResourceBlock("cpu", target=Target(average_utilization=80)))],
    }])
    out = _format_snapshot(data)
    assert "AT MAX" in out
    assert "(cpu 12%/target 80%)" in out


def test_resource_cpu_target_in_bare_cores_is_normalized_to_millicores():
    """The regression: a bare-core target printed against a millicore current.

    A KEDA-managed HPA can express its CPU target as the Quantity "1" (one
    core) while the current side arrives as "4m", which read as 'cpu 4m/target
    1' looks like a capacity problem three orders of magnitude off.
    """
    hpa = {
        "current_metrics": [Entry("Resource", ResourceBlock("cpu", current=Target(average_value="4m")))],
        "target_metrics": [Entry("Resource", ResourceBlock("cpu", target=Target(average_value="1")))],
    }
    out = _format_hpa_metrics(hpa)
    assert "cpu 4m/target 1000m" in out
    assert not re.search(r"cpu 4m/target 1(?!\d)", out)


def test_resource_memory_quantities_are_normalized_on_both_sides():
    hpa = {
        "current_metrics": [Entry("Resource", ResourceBlock("memory", current=Target(average_value="524288Ki")))],
        "target_metrics": [Entry("Resource", ResourceBlock("memory", target=Target(average_value="2Gi")))],
    }
    assert _format_hpa_metrics(hpa) == " (memory 512Mi/target 2.0Gi)"


def test_non_resource_metric_keeps_its_bare_number():
    """pods/object/external metrics are dimensionless, so nothing to normalize."""
    hpa = {
        "current_metrics": [Entry("External", ExternalBlock("s1-postgresql", current=Target(average_value="0")))],
        "target_metrics": [Entry("External", ExternalBlock("s1-postgresql", target=Target(average_value="7")))],
    }
    assert _format_hpa_metrics(hpa) == " (s1-postgresql 0/target 7)"


def test_unparseable_resource_quantity_is_tagged_rather_than_compared():
    hpa = {
        "current_metrics": [Entry("Resource", ResourceBlock("cpu", current=Target(average_value="4m")))],
        "target_metrics": [Entry("Resource", ResourceBlock("cpu", target=Target(average_value="12x")))],
    }
    out = _format_hpa_metrics(hpa)
    assert "cpu 4m/target 12x (units unverified)" in out


def test_hpa_without_metrics_renders_exactly_as_before():
    data = base_data(hpas=[{
        "namespace": "prod", "name": "api", "min": 1, "max": 4,
        "current": 2, "desired": 2,
    }])
    out = _format_snapshot(data)
    assert "prod/api  2/4" in out
    assert "(" not in out.split("=== HPAs ===")[1]


# ---------------------------------------------------------------------------
# Quantity normalization
# ---------------------------------------------------------------------------

from scheduler import _cpu_millicores, _fmt_mem_ki, _fmt_utilization, _memory_ki


@pytest.mark.parametrize("raw,expected", [
    ("244354044n", 244.354044),   # metrics API nanocores
    ("15890m", 15890.0),          # node allocatable millicores
    ("16", 16000.0),              # bare cores
    ("500u", 0.5),                # microcores
    ("12x", None),                # unknown suffix, never guessed
    ("", None),
    (None, None),
])
def test_cpu_millicores(raw, expected):
    got = _cpu_millicores(raw)
    assert got is None if expected is None else abs(got - expected) < 1e-6


@pytest.mark.parametrize("raw,expected", [
    ("12046320Ki", 12046320),
    ("512Mi", 524288),
    ("2Gi", 2097152),
    ("5Pi", None),                # unsupported unit
    ("", None),
])
def test_memory_ki(raw, expected):
    assert _memory_ki(raw) == expected


def test_fmt_utilization_degrades_without_capacity():
    fmt = lambda v: f"{v:.0f}m"
    assert _fmt_utilization(244.35, 15890.0, fmt) == "244m/15890m (2%)"
    assert _fmt_utilization(244.35, None, fmt) == "244m"   # capacity unknown
    assert _fmt_utilization(None, 15890.0, fmt) == "?"     # usage unknown


def test_fmt_mem_ki_picks_a_sensible_unit():
    assert _fmt_mem_ki(12046320) == "11.5Gi"
    assert _fmt_mem_ki(524288) == "512Mi"
    assert _fmt_mem_ki(900) == "900Ki"
    assert _fmt_mem_ki(None) == "?"


# ---------------------------------------------------------------------------
# Pod utilization rendering
# ---------------------------------------------------------------------------

def test_node_utilization_shows_a_percentage_when_capacity_is_known():
    data = base_data(
        nodes=[{"name": "n1", "status": "Ready", "version": "v1.30",
                "cpu_allocatable": "15890m", "memory_allocatable": "61346264Ki"}],
        node_metrics=[{"name": "n1", "cpu": "244354044n", "memory": "12046320Ki"}],
    )
    out = _format_snapshot(data)
    assert "cpu=244m/15890m (2%)" in out
    assert "memory=11.5Gi/58.5Gi (20%)" in out


def test_node_utilization_falls_back_when_capacity_is_missing():
    data = base_data(
        nodes=[{"name": "n1", "status": "Ready", "version": "v1.30"}],
        node_metrics=[{"name": "n1", "cpu": "244354044n", "memory": "12046320Ki"}],
    )
    out = _format_snapshot(data)
    assert "cpu=244m  memory=11.5Gi" in out
    assert "%" not in out.split("NODE UTILIZATION")[1].split("\n")[1]


def test_unhealthy_pod_lines_carry_usage_when_available():
    data = base_data(
        unhealthy_pods=[{"namespace": "prod", "name": "api-1",
                         "status": "CrashLoopBackOff", "restarts": 12, "age": "3h"}],
        pod_metrics={"prod/api-1": {"cpu_n": 182344000, "memory_ki": 524288}},
    )
    out = _format_snapshot(data)
    assert "CrashLoopBackOff" in out
    assert "cpu=182m mem=512Mi" in out


def test_unhealthy_pod_without_metrics_renders_as_before():
    data = base_data(
        unhealthy_pods=[{"namespace": "prod", "name": "api-1",
                         "status": "OOMKilled", "restarts": 3, "age": "1h"}],
    )
    out = _format_snapshot(data)
    assert "prod/api-1  OOMKilled  restarts=3  age=1h" in out
    assert "cpu=" not in out.split("UNHEALTHY PODS")[1]


def test_top_pods_section_is_capped_and_sorted():
    metrics = {f"ns/pod-{i}": {"cpu_n": i * 1_000_000, "memory_ki": 1024}
               for i in range(1, 26)}
    out = _format_snapshot(base_data(pod_metrics=metrics))
    assert "=== TOP PODS BY CPU (top 10 of 25) ===" in out
    section = out.split("TOP PODS BY CPU")[1]
    assert "pod-25  cpu=25m" in section          # busiest first
    assert "pod-1  cpu=1m" not in section        # quietest excluded
    assert section.count("cpu=") == 10           # hard cap


def test_top_pods_section_absent_without_metrics():
    assert "TOP PODS BY CPU" not in _format_snapshot(base_data())


# ---------------------------------------------------------------------------
# HealthReport validation and repair
# ---------------------------------------------------------------------------

from schemas import HealthReport
from scheduler import _coerce_severity, _repair_health_report


def test_all_info_report_is_now_representable():
    """The regression: 'highest severity among findings' can be info.

    overall_severity previously allowed only critical/warning/ok, so a report
    whose worst finding was info had no valid value and failed validation.
    """
    r = HealthReport(
        overall_severity="info",
        summary="Only optimization opportunities.",
        findings=[{"severity": "info", "title": "Over-provisioned",
                   "detail": "requests far exceed usage", "namespace": "prod",
                   "kind": "Deployment", "resource_name": "api",
                   "reason": "OverProvisioned"}],
    )
    assert r.overall_severity == "info"
    assert not r.has_issues          # info is not an actionable issue


def test_every_finding_severity_is_a_valid_overall_severity():
    """Guards the enum drift that caused the failure in the first place."""
    import typing
    from schemas import OverallSeverity, Severity
    assert set(typing.get_args(Severity)) <= set(typing.get_args(OverallSeverity))


@pytest.mark.parametrize("raw,expected", [
    ("critical", "critical"),
    ("CRITICAL", "critical"),
    ("high", "critical"),
    ("medium", "warning"),
    ("low", "info"),
    ("nonsense", "info"),          # falls back to the default
    (None, "info"),
])
def test_coerce_severity(raw, expected):
    assert _coerce_severity(raw, ("critical", "warning", "info"), "info") == expected


def test_repair_salvages_a_report_with_a_drifted_overall_severity():
    report = _repair_health_report({
        "overall_severity": "high",     # not in the vocabulary
        "summary": "Something is wrong.",
        "findings": [{"severity": "critical", "title": "t", "detail": "d"}],
    })
    assert report is not None
    assert report.overall_severity == "critical"
    assert len(report.findings) == 1   # finding preserved, not discarded


def test_repair_derives_overall_severity_when_missing():
    report = _repair_health_report({
        "summary": "s",
        "findings": [
            {"severity": "info", "title": "a", "detail": "d"},
            {"severity": "warning", "title": "b", "detail": "d"},
        ],
    })
    assert report.overall_severity == "warning"   # highest among findings


def test_repair_drops_only_the_unsalvageable_finding():
    report = _repair_health_report({
        "overall_severity": "warning",
        "summary": "s",
        "findings": [
            {"severity": "warning", "title": "good", "detail": "d"},
            {"severity": "warning", "title": "missing detail"},   # invalid
            "not even a dict",
        ],
    })
    assert [f.title for f in report.findings] == ["good"]


def test_repair_of_a_healthy_report_yields_ok():
    report = _repair_health_report({"summary": "All good.", "findings": []})
    assert report.overall_severity == "ok"
    assert not report.has_issues


def test_repair_returns_none_for_unusable_payloads():
    assert _repair_health_report(None) is None
    assert _repair_health_report("a string") is None


def test_repair_supplies_a_summary_when_absent():
    report = _repair_health_report({"overall_severity": "ok", "findings": []})
    assert report.summary == "Health check completed."
