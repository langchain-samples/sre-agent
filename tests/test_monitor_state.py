"""Tests for stateful monitoring: finding identity and run-to-run diffing.

These cover the two things that decide whether the monitoring loop is usable:
identity must survive the model rewording a title and Kubernetes recycling a pod
name, and the diff must only surface what a human has not already seen.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from monitor_state import (
    ReportDiff,
    StoredFinding,
    diff_report,
    fingerprint,
    humanize_age,
    normalize_resource_name,
    parse_fingerprint,
)
from schemas import Finding, HealthReport

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def finding(
    severity="critical",
    title="CrashLoopBackOff on api",
    namespace="prod",
    kind="Pod",
    resource_name="api-6b474476c4-6nqxr",
    reason="CrashLoopBackOff",
    detail="container exits 1",
):
    return Finding(
        severity=severity, title=title, detail=detail, namespace=namespace,
        kind=kind, resource_name=resource_name, reason=reason,
    )


def report(*findings, severity="critical"):
    return HealthReport(
        overall_severity=severity, summary="s", findings=list(findings),
    )


def stored(fp, severity="critical", first_seen=None, times_seen=3,
           resolved_at=None, ack_until=None):
    return StoredFinding(
        fingerprint=fp, severity=severity, title="t", namespace="prod",
        first_seen=first_seen or (NOW - timedelta(hours=6)),
        last_seen=NOW - timedelta(minutes=30), times_seen=times_seen,
        resolved_at=resolved_at, ack_until=ack_until,
    )


# ---------------------------------------------------------------------------
# Resource name normalization
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind,name,expected", [
    # Deployment pods: strip replicaset hash + pod suffix.
    ("Pod", "nginx-deployment-6b474476c4-6nqxr", "nginx-deployment"),
    ("Pod", "coredns-5d78c9869d-vwq2t", "coredns"),
    # DaemonSet / Job / bare-ReplicaSet pods: strip the single suffix.
    ("Pod", "log-shipper-4tzvn", "log-shipper"),
    # StatefulSet ordinals are part of the identity and must survive.
    ("Pod", "web-0", "web-0"),
    ("Pod", "postgres-2", "postgres-2"),
    # A bare name is left alone.
    ("Pod", "redis", "redis"),
    # Non-pod kinds are never rewritten, even if they look suffixed.
    ("Deployment", "api-6b474476c4", "api-6b474476c4"),
    ("Node", "ip-10-0-1-23.ec2.internal", "ip-10-0-1-23.ec2.internal"),
    ("", "", ""),
])
def test_normalize_resource_name(kind, name, expected):
    assert normalize_resource_name(kind, name) == expected


def test_normalize_preserves_names_with_vowels_and_zeros():
    """Real k8s suffixes exclude vowels, 0 and 1, so words are never eaten."""
    # "cache" is 5 chars but contains vowels -> not a generated suffix.
    assert normalize_resource_name("Pod", "redis-cache") == "redis-cache"
    # "10001" contains 0 and 1 -> not a generated suffix.
    assert normalize_resource_name("Pod", "shard-10001") == "shard-10001"


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------

def test_fingerprint_is_stable_when_the_model_rewords_the_title():
    """The whole reason identity fields exist: titles drift, identity must not."""
    a = finding(title="CrashLoopBackOff on api-6b474476c4-6nqxr")
    b = finding(title="api is crash looping (restarts=240)", detail="different words")
    assert fingerprint(a) == fingerprint(b)


def test_fingerprint_is_stable_across_pod_recreation():
    """A restarted pod gets a new random name but is the same incident."""
    before = finding(resource_name="api-6b474476c4-6nqxr")
    after = finding(resource_name="api-6b474476c4-9tzvn")
    assert fingerprint(before) == fingerprint(after)


def test_fingerprint_distinguishes_different_objects_and_reasons():
    base = finding()
    assert fingerprint(base) != fingerprint(finding(resource_name="worker-5d78c9869d-vwq2t"))
    assert fingerprint(base) != fingerprint(finding(reason="OOMKilled"))
    assert fingerprint(base) != fingerprint(finding(namespace="staging"))


def test_fingerprint_falls_back_to_title_without_identity_fields():
    """Degrades rather than raising when the model omits every identity field."""
    bare = Finding(severity="info", title="Cluster has no NetworkPolicies", detail="d")
    fp = fingerprint(bare)
    assert fp.endswith("cluster-has-no-networkpolicies")
    assert fingerprint(bare) == fp  # deterministic


def test_fingerprint_ignores_severity():
    """Severity change must be an escalation of one finding, not a new one."""
    assert fingerprint(finding(severity="warning")) == fingerprint(finding(severity="critical"))


def test_parse_fingerprint_recovers_namespace_kind_and_normalized_name():
    f = finding(namespace="prod", kind="Pod", resource_name="api-6b474476c4-6nqxr", reason="CrashLoopBackOff")
    assert parse_fingerprint(fingerprint(f)) == ("prod", "pod", "api")


def test_parse_fingerprint_returns_none_for_the_no_identity_fallback():
    bare = Finding(severity="info", title="Cluster has no NetworkPolicies", detail="d")
    assert parse_fingerprint(fingerprint(bare)) is None


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------

def test_unseen_finding_is_new_and_notifies():
    f = finding()
    diff = diff_report(report(f), {}, NOW)
    assert [d.fingerprint for d in diff.new] == [fingerprint(f)]
    assert diff.new[0].times_seen == 1
    assert diff.new[0].first_seen == NOW
    assert diff.should_notify()


def test_repeat_finding_is_ongoing_and_does_not_notify():
    """The core anti-noise property: nothing changed, so stay quiet."""
    f = finding()
    fp = fingerprint(f)
    diff = diff_report(report(f), {fp: stored(fp, times_seen=3)}, NOW)
    assert not diff.new and not diff.escalated and not diff.resolved
    assert len(diff.ongoing) == 1
    assert diff.ongoing[0].times_seen == 4          # incremented
    assert diff.ongoing[0].age == timedelta(hours=6)  # first_seen preserved
    assert not diff.should_notify()


def test_severity_increase_is_an_escalation_and_notifies():
    f = finding(severity="critical")
    fp = fingerprint(f)
    diff = diff_report(report(f), {fp: stored(fp, severity="warning")}, NOW)
    assert len(diff.escalated) == 1
    assert diff.escalated[0].previous_severity == "warning"
    assert diff.should_notify()


def test_severity_decrease_is_not_an_escalation():
    f = finding(severity="warning")
    fp = fingerprint(f)
    diff = diff_report(report(f), {fp: stored(fp, severity="critical")}, NOW)
    assert not diff.escalated
    assert len(diff.ongoing) == 1
    assert not diff.should_notify()


def test_disappeared_finding_is_resolved():
    fp = "prod/pod/api:crashloopbackoff"
    diff = diff_report(report(), {fp: stored(fp)}, NOW)
    assert [r.fingerprint for r in diff.resolved] == [fp]
    assert diff.should_notify()
    # ...unless the operator does not want recovery notifications.
    assert not diff.should_notify(notify_on_resolved=False)


def test_already_resolved_finding_is_not_resolved_twice():
    fp = "prod/pod/api:crashloopbackoff"
    prev = stored(fp, resolved_at=NOW - timedelta(hours=1))
    diff = diff_report(report(), {fp: prev}, NOW)
    assert not diff.resolved
    assert not diff.should_notify()


def test_returning_finding_is_new_again_but_keeps_its_history():
    """A flapping deployment should re-alert, and say it is a repeat offender."""
    f = finding()
    fp = fingerprint(f)
    prev = stored(fp, times_seen=7, resolved_at=NOW - timedelta(hours=2))
    diff = diff_report(report(f), {fp: prev}, NOW)
    assert len(diff.new) == 1
    assert diff.new[0].times_seen == 8      # cumulative across the flap
    assert diff.new[0].first_seen == NOW    # but the clock restarts
    assert diff.should_notify()


def test_healthy_cluster_with_no_history_is_silent():
    diff = diff_report(report(severity="ok"), {}, NOW)
    assert not diff.should_notify()
    assert diff.summary_line() == "no findings"


# ---------------------------------------------------------------------------
# Acks
# ---------------------------------------------------------------------------

def test_acked_finding_is_suppressed_not_reported():
    f = finding()
    fp = fingerprint(f)
    prev = stored(fp, ack_until=NOW + timedelta(hours=12))
    diff = diff_report(report(f), {fp: prev}, NOW)
    assert not diff.new and not diff.ongoing and not diff.escalated
    assert len(diff.suppressed) == 1
    assert not diff.should_notify()
    # Still tracked so history stays correct once the ack lapses.
    assert diff.suppressed[0].times_seen == 4


def test_expired_ack_reports_normally_again():
    f = finding()
    fp = fingerprint(f)
    prev = stored(fp, ack_until=NOW - timedelta(minutes=1))
    diff = diff_report(report(f), {fp: prev}, NOW)
    assert len(diff.ongoing) == 1
    assert not diff.suppressed


def test_escalation_of_an_acked_finding_stays_suppressed():
    """An ack means "not now" — it should not be defeated by a severity bump."""
    f = finding(severity="critical")
    fp = fingerprint(f)
    prev = stored(fp, severity="warning", ack_until=NOW + timedelta(hours=5))
    diff = diff_report(report(f), {fp: prev}, NOW)
    assert not diff.escalated
    assert len(diff.suppressed) == 1


def test_acked_finding_resolving_is_silent():
    fp = "prod/pod/api:crashloopbackoff"
    prev = stored(fp, ack_until=NOW + timedelta(hours=5))
    diff = diff_report(report(), {fp: prev}, NOW)
    assert not diff.resolved
    assert not diff.should_notify()


# ---------------------------------------------------------------------------
# Within-run dedupe and ordering
# ---------------------------------------------------------------------------

def test_duplicate_findings_in_one_run_collapse_to_the_worst():
    """Two findings for one object must not double-count times_seen."""
    mild = finding(severity="warning", title="api restarting")
    severe = finding(severity="critical", title="api crash looping")
    diff = diff_report(report(mild, severe), {}, NOW)
    assert len(diff.new) == 1
    assert diff.new[0].finding.severity == "critical"


def test_active_findings_are_ordered_by_severity():
    diff = diff_report(
        report(
            finding(severity="info", resource_name="a-4tzvn", reason="R1"),
            finding(severity="critical", resource_name="b-4tzvn", reason="R2"),
            finding(severity="warning", resource_name="c-4tzvn", reason="R3"),
        ),
        {}, NOW,
    )
    assert [d.finding.severity for d in diff.new] == ["critical", "warning", "info"]


def test_summary_line_reports_every_bucket():
    f = finding()
    fp = fingerprint(f)
    gone = "prod/pod/old:notready"
    diff = diff_report(report(f), {gone: stored(gone)}, NOW)
    assert "1 new" in diff.summary_line()
    assert "1 resolved" in diff.summary_line()


def test_empty_diff_notifies_nothing():
    assert not ReportDiff().should_notify()


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("delta,expected", [
    (None, "unknown"),
    (timedelta(seconds=5), "just now"),
    (timedelta(minutes=45), "45m"),
    (timedelta(hours=6), "6h"),
    (timedelta(days=3, hours=2), "3d"),
])
def test_humanize_age(delta, expected):
    assert humanize_age(delta) == expected
