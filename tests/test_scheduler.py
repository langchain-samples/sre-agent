"""Tests for the scheduler's snapshot formatting."""
from scheduler import _format_snapshot


def _snapshot_with_hpas(hpas: list[dict]) -> str:
    return _format_snapshot({
        "nodes": [],
        "pods": [],
        "unhealthy_pods": [],
        "events": [],
        "hpas": hpas,
        "deployments": [],
        "errors": [],
    })


def test_fixed_scale_hpa_is_not_flagged_at_max():
    out = _snapshot_with_hpas([
        {"namespace": "keda", "name": "pinned", "min": 1, "max": 1, "current": 1, "desired": 1},
    ])
    assert "AT MAX" not in out
    assert "(fixed scale, cannot autoscale)" in out
    assert "min=1" in out


def test_saturated_scalable_hpa_is_flagged_at_max():
    out = _snapshot_with_hpas([
        {"namespace": "web", "name": "api", "min": 1, "max": 10, "current": 10, "desired": 10},
    ])
    assert "AT MAX" in out
    assert "(fixed scale, cannot autoscale)" not in out


def test_scalable_hpa_below_ceiling_has_no_markers():
    out = _snapshot_with_hpas([
        {"namespace": "web", "name": "api", "min": 1, "max": 10, "current": 1, "desired": 1},
    ])
    assert "AT MAX" not in out
    assert "(fixed scale, cannot autoscale)" not in out


def test_hpa_with_unset_replica_bounds_does_not_raise():
    out = _snapshot_with_hpas([
        {"namespace": "web", "name": "api", "min": None, "max": None, "current": "?", "desired": "?"},
    ])
    assert "AT MAX" not in out
