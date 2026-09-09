"""Tests for pod health classification and warning-event freshness.

Both of these produced sustained false criticals against a healthy cluster, so the
cases below are mostly about what must NOT be reported.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from scheduler import (
    EVENT_MAX_AGE_MINUTES,
    POD_FAILURE_RECENCY_MINUTES,
    POD_RESTART_NOTABLE,
    POD_STARTUP_GRACE_MINUTES,
    _classify_pod,
    _format_snapshot,
    _minutes_since,
)

NOW = datetime(2026, 8, 11, 22, 0, tzinfo=timezone.utc)


def container(ready=True, restarts=0, waiting=None,
              terminated=None, last_terminated=None):
    def term(spec):
        if spec is None:
            return None
        reason, exit_code, mins_ago = spec
        return NS(reason=reason, exit_code=exit_code,
                  finished_at=NOW - timedelta(minutes=mins_ago))
    return NS(
        ready=ready,
        restart_count=restarts,
        state=NS(waiting=NS(reason=waiting) if waiting else None,
                 terminated=term(terminated), running=None),
        last_state=NS(terminated=term(last_terminated), waiting=None, running=None),
    )


def pod(phase="Running", age_minutes=600, containers=None):
    return NS(
        metadata=NS(namespace="langsmith", name="api-1",
                    creation_timestamp=NOW - timedelta(minutes=age_minutes)),
        status=NS(phase=phase, container_statuses=containers or [container()]),
    )


# ---------------------------------------------------------------------------
# The regression: cumulative restart count is not a fault signal
# ---------------------------------------------------------------------------

def test_high_restart_count_with_clean_exits_is_healthy():
    """The actual bug. 43 lifetime restarts from rolling redeploys, all exit 0,
    pod Running and Ready, was reported critical for hours."""
    p = pod(containers=[container(ready=True, restarts=43,
                                  last_terminated=("Completed", 0, 150))])
    unhealthy, status, extra = _classify_pod(p, NOW)
    assert unhealthy is False
    assert status == "Running"
    assert extra["last_termination"] == "Completed"


@pytest.mark.parametrize("restarts", [0, 1, 5, 20, 43, 500])
def test_restart_count_alone_never_makes_a_pod_unhealthy(restarts):
    p = pod(containers=[container(ready=True, restarts=restarts,
                                 last_terminated=("Completed", 0, 200))])
    assert _classify_pod(p, NOW)[0] is False


def test_healthy_pod_that_never_restarted():
    assert _classify_pod(pod(), NOW)[0] is False


# ---------------------------------------------------------------------------
# Genuine faults must still be caught
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reason", [
    "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull",
    "InvalidImageName", "CreateContainerConfigError",
])
def test_stuck_waiting_reasons_are_unhealthy(reason):
    p = pod(containers=[container(ready=False, waiting=reason)])
    unhealthy, status, _ = _classify_pod(p, NOW)
    assert unhealthy is True
    assert status == reason


def test_recent_oomkill_is_unhealthy_even_though_running_again():
    p = pod(containers=[container(ready=True, restarts=3,
                                  last_terminated=("OOMKilled", 137, 5))])
    unhealthy, status, _ = _classify_pod(p, NOW)
    assert unhealthy is True
    assert "OOMKilled" in status


def test_old_oomkill_is_no_longer_current():
    """Past the recency window a recovered pod is history, not an incident."""
    p = pod(containers=[container(ready=True, restarts=3,
                                 last_terminated=("OOMKilled", 137,
                                                  POD_FAILURE_RECENCY_MINUTES + 30))])
    assert _classify_pod(p, NOW)[0] is False


def test_nonzero_exit_counts_as_failure_even_with_an_odd_reason():
    p = pod(containers=[container(ready=True, last_terminated=("Unknown", 1, 5))])
    assert _classify_pod(p, NOW)[0] is True


def test_running_but_not_ready_past_grace_is_unhealthy():
    p = pod(containers=[container(ready=False)])
    unhealthy, status, _ = _classify_pod(p, NOW)
    assert unhealthy is True
    assert status == "NotReady"


def test_not_ready_within_grace_is_still_starting():
    p = pod(age_minutes=POD_STARTUP_GRACE_MINUTES - 1,
            containers=[container(ready=False)])
    assert _classify_pod(p, NOW)[0] is False


def test_pending_past_grace_is_unhealthy_but_not_while_starting():
    assert _classify_pod(pod(phase="Pending", age_minutes=60), NOW)[0] is True
    assert _classify_pod(pod(phase="Pending", age_minutes=1), NOW)[0] is False


def test_succeeded_pods_are_never_unhealthy():
    p = pod(phase="Succeeded", containers=[container(ready=False, restarts=99)])
    assert _classify_pod(p, NOW)[0] is False


def test_recently_failed_pod_is_unhealthy_but_an_old_one_is_not():
    recent = pod(phase="Failed", containers=[container(ready=False, last_terminated=("Error", 1, 5))])
    old = pod(phase="Failed", containers=[
        container(ready=False, last_terminated=("Error", 1, POD_FAILURE_RECENCY_MINUTES + 60))])
    assert _classify_pod(recent, NOW)[0] is True
    assert _classify_pod(old, NOW)[0] is False


def test_kube_system_is_not_exempt():
    """The old classifier carved out kube-system, the one namespace where a
    broken pod matters most."""
    p = pod(containers=[container(ready=False, waiting="CrashLoopBackOff")])
    p.metadata.namespace = "kube-system"
    assert _classify_pod(p, NOW)[0] is True


# ---------------------------------------------------------------------------
# Snapshot rendering
# ---------------------------------------------------------------------------

def base_data(**over):
    data = {
        "nodes": [], "pods": [], "unhealthy_pods": [], "events": [], "hpas": [],
        "deployments": [], "node_metrics": [], "pod_metrics": {}, "pvc_usage": {},
        "errors": [],
    }
    data.update(over)
    return data


def pod_row(name="api-1", restarts=0, status="Running", last=None, ago=None):
    return {"namespace": "langsmith", "name": name, "status": status,
            "restarts": restarts, "age": "10d", "ready": True,
            "last_termination": last, "last_exit_code": 0,
            "last_termination_min_ago": ago}


def test_high_restart_pods_render_as_context_not_as_unhealthy():
    rows = [pod_row(f"api-{i}", restarts=POD_RESTART_NOTABLE + i,
                    last="Completed", ago=150) for i in range(3)]
    out = _format_snapshot(base_data(pods=rows))
    assert "=== PODS === all 3 pods healthy" in out
    assert "HIGH RESTART COUNTS (clean exits, not currently failing)" in out
    assert "Completed" in out
    assert "UNHEALTHY PODS" not in out


def test_unhealthy_pod_line_states_the_cause():
    row = pod_row("api-1", restarts=4, status="Restarted/OOMKilled",
                  last="OOMKilled", ago=5)
    row["last_exit_code"] = 137
    out = _format_snapshot(base_data(pods=[row], unhealthy_pods=[row]))
    assert "=== UNHEALTHY PODS ===" in out
    assert "last=OOMKilled(exit 137) 5m ago" in out


def test_low_restart_pods_get_no_context_section():
    out = _format_snapshot(base_data(pods=[pod_row(restarts=2)]))
    assert "HIGH RESTART COUNTS" not in out


# ---------------------------------------------------------------------------
# Warning-event freshness
# ---------------------------------------------------------------------------

def test_event_section_states_its_window():
    events = [{"namespace": "prod", "object": "Pod/api-1", "reason": "BackOff",
               "message": "failing", "count": 3, "age_min": 4}]
    out = _format_snapshot(base_data(events=events))
    assert f"=== WARNING EVENTS (last {EVENT_MAX_AGE_MINUTES}m) ===" in out


def test_each_event_carries_its_age():
    """Without an age, a 51-minute-old warning about a deleted pod read as live."""
    events = [{"namespace": "prod", "object": "Pod/gone", "reason": "Failed",
               "message": "boom", "count": 1, "age_min": 51}]
    out = _format_snapshot(base_data(events=events))
    assert "51m ago" in out


def test_event_with_unknown_age_says_so():
    events = [{"namespace": "prod", "object": "Pod/x", "reason": "Failed",
               "message": "m", "count": 1, "age_min": None}]
    assert "age unknown" in _format_snapshot(base_data(events=events))


def test_warning_events_group_repeated_objects_and_preserve_messages():
    ingress_message = "AWS ELBv2 error code Target.ResponseCodeMismatch"
    events = [
        {"namespace": "langsmith", "object": "Ingress/langsmith-ingress",
         "reason": "FailedDeployModel", "message": ingress_message,
         "count": 1, "age_min": age}
        for age in range(11, 19)
    ]
    events.extend([
        {"namespace": "prod", "object": "HPA/api", "reason": "FailedGetMetrics",
         "message": "unable to fetch metrics: metrics API unavailable",
         "count": 1, "age_min": 20},
        {"namespace": "prod", "object": "Pod/api", "reason": "Unhealthy",
         "message": "Readiness probe connection refused",
         "count": 1, "age_min": 21},
        {"namespace": "prod", "object": "Deployment/api", "reason": "Failed",
         "message": "deployment rollout exceeded progress deadline",
         "count": 1, "age_min": 22},
    ])

    out = _format_snapshot(base_data(events=events))

    assert out.count("Ingress/langsmith-ingress") == 1
    assert "FailedDeployModel (x8, 18-11m ago): " + ingress_message in out
    assert "HPA/api" in out
    assert "Pod/api" in out
    assert "Deployment/api" in out
    assert ingress_message in out
    assert "metrics API unavailable" in out
    assert "Readiness probe connection refused" in out
    assert "rollout exceeded progress deadline" in out


def test_minutes_since_handles_a_missing_timestamp():
    assert _minutes_since(None, NOW) == float("inf")
    assert _minutes_since(NOW - timedelta(minutes=30), NOW) == pytest.approx(30, abs=0.01)
