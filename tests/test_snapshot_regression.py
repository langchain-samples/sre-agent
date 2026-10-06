"""Replay recorded cluster states through the real detection code.

The prose-scenario dataset in evals/ tests reasoning: given a described incident,
does the agent call the right tools and say the right things. It cannot catch a
collector that mis-classifies a healthy pod, because it never involves cluster
state.

These fixtures do. Each one is a real (or faithfully reconstructed) cluster
snapshot with a hand-written expectation, replayed through the production
_classify_pod and _format_snapshot. Every bug shipped so far would have been
caught here.

Deterministic by construction: fixtures store relative timestamps, replayed
against a fixed `now`, so they never rot. No cluster and no LLM call.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from evals.snapshot_fixture import all_fixtures, load, replay
from scheduler import _format_snapshot

FIXED_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
FIXTURES = all_fixtures()
SEVERITY_RANK = {"ok": 0, "info": 1, "warning": 2, "critical": 3}


def ids(fixtures):
    return [name for name, _ in fixtures]


@pytest.mark.parametrize("name,fixture", FIXTURES, ids=ids(FIXTURES))
def test_fixture_classifies_as_expected(name, fixture):
    """The classifier's verdict must match the hand-written expectation."""
    data = replay(fixture, FIXED_NOW)
    actual = sorted(f"{p['namespace']}/{p['name']}" for p in data["unhealthy_pods"])
    expected = sorted(fixture["expected"]["unhealthy_pod_names"])
    assert actual == expected, (
        f"\n  fixture: {name}"
        f"\n  note   : {fixture.get('note','')[:160]}"
        f"\n  expected unhealthy: {expected}"
        f"\n  actual   unhealthy: {actual}"
    )


@pytest.mark.parametrize("name,fixture", FIXTURES, ids=ids(FIXTURES))
def test_fixture_snapshot_contains_required_strings(name, fixture):
    out = _format_snapshot(replay(fixture, FIXED_NOW))
    for needle in fixture["expected"].get("snapshot_must_contain", []):
        assert needle in out, f"{name}: snapshot missing {needle!r}"


@pytest.mark.parametrize("name,fixture", FIXTURES, ids=ids(FIXTURES))
def test_fixture_snapshot_omits_forbidden_strings(name, fixture):
    """Catches stale data being presented as current fact."""
    out = _format_snapshot(replay(fixture, FIXED_NOW))
    for needle in fixture["expected"].get("snapshot_must_not_contain", []):
        assert needle not in out, f"{name}: snapshot should not mention {needle!r}"


@pytest.mark.parametrize("name,fixture", FIXTURES, ids=ids(FIXTURES))
def test_fixture_is_deterministic_across_replays(name, fixture):
    """Relative timestamps mean two replays at different `now` values agree."""
    a = replay(fixture, FIXED_NOW)
    b = replay(fixture, FIXED_NOW.replace(year=2030))
    assert ([p["status"] for p in a["pods"]] == [p["status"] for p in b["pods"]])
    assert len(a["unhealthy_pods"]) == len(b["unhealthy_pods"])


@pytest.mark.parametrize("name,fixture", FIXTURES, ids=ids(FIXTURES))
def test_fixture_carries_no_event_messages(name, fixture):
    """Event text is free-form controller output and is dropped at capture."""
    for e in fixture["collected"].get("events", []):
        assert not e.get("message"), f"{name}: event message should be empty"


def test_there_is_at_least_one_fixture():
    assert FIXTURES, "no fixtures found; evals/fixtures/ is empty"


# ---------------------------------------------------------------------------
# The specific regression this suite exists for
# ---------------------------------------------------------------------------

def test_rolling_restart_is_not_an_outage():
    """43 lifetime restarts with clean exits is a redeploy, not a fleet outage.

    This exact state drove severity=critical with 10 unhealthy pods for hours.
    """
    fixture = load("rolling-restart-false-critical")
    data = replay(fixture, FIXED_NOW)

    assert data["unhealthy_pods"] == []
    assert len(data["pods"]) == 10
    assert all(p["restarts"] >= 18 for p in data["pods"])
    assert all(p["last_termination"] == "Completed" for p in data["pods"])

    out = _format_snapshot(data)
    assert "all 10 pods healthy" in out
    assert "UNHEALTHY PODS" not in out
    assert "HIGH RESTART COUNTS" in out          # signal kept as context
    assert "sre-agent-7f47d67cf4" not in out     # stale event about a deleted pod
