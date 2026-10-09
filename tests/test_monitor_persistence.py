"""Replay normalized reports through the persistence boundary without Postgres."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

import scheduler
from monitor_state import diff_report, fingerprint
from persistence import PostgresDatabase, SCHEMA
from schemas import Finding, HealthReport


@pytest.fixture
def db():
    rows = {}
    conn = MagicMock()
    pool = MagicMock()
    pool.connection.return_value.__enter__.return_value = conn

    def execute(query, params):
        if "INSERT INTO finding_state" in query:
            assert "severity      = EXCLUDED.severity" in query
            assert "pending_severity = EXCLUDED.pending_severity" in query
            keys = (
                "fingerprint", "namespace", "kind", "resource_name", "reason",
                "severity", "title", "detail", "first_seen", "last_seen",
                "times_seen", "pending_severity",
            )
            row = dict(zip(keys, params, strict=True))
            row.update(resolved_at=None, ack_until=None)
            rows[row["fingerprint"]] = row
        elif "SELECT fingerprint" in query:
            return MagicMock(fetchall=lambda: list(rows.values()))
        elif "UPDATE finding_state" in query:
            assert "pending_severity = NULL" in query
            for identity in params[1]:
                rows[identity].update(resolved_at=params[0], pending_severity=None)
        else:
            pytest.fail(f"Unexpected persistence query: {query}")

    conn.execute.side_effect = execute
    return PostgresDatabase(pool)


def test_schema_migrates_existing_finding_state():
    assert "ALTER TABLE finding_state ADD COLUMN IF NOT EXISTS pending_severity text" in SCHEMA


def test_replay_normalization_confirmation_and_resolution(db, monkeypatch):
    now = datetime(2026, 10, 9, tzinfo=timezone.utc)
    observations = [
        ("warning", "new", "warning", None, True),
        ("critical", "ongoing", "warning", "critical", False),
        ("warning", "ongoing", "warning", None, False),
        ("critical", "ongoing", "warning", "critical", False),
        ("critical", "escalated", "critical", None, True),
        ("info", "ongoing", "info", None, False),
        ("warning", "ongoing", "info", "warning", False),
        ("critical", "ongoing", "info", "critical", False),
        (None, "resolved", "info", None, True),
        ("critical", "new", "critical", None, True),
        ("critical", "ongoing", "critical", None, False),
    ]
    identity = None
    for severity, status, comparison, pending, notify in observations:
        payload = {
            "overall_severity": "warning", "summary": "Snapshot summary",
            "findings": [] if severity is None else [{
                "severity": severity, "title": "Autoscaling blocked", "detail": "Selector overlaps",
                "namespace": "prod", "kind": "HPA", "resource_name": "api",
                "reason": "AmbiguousSelector",
            }],
        }
        monkeypatch.setattr(scheduler, "request_health_report", lambda *args: payload)
        report = scheduler._analyse_snapshot("unchanged snapshot")
        assert report.overall_severity == (severity or "ok")
        if report.findings:
            identity = fingerprint(report.findings[0])
        diff = diff_report(report, db.load_tracked_findings(), now)
        assert len(getattr(diff, status)) == 1
        assert diff.should_notify() is notify
        db.apply_diff(diff, now)
        tracked = db.load_tracked_findings()[identity]
        assert tracked.severity == comparison
        assert tracked.pending_severity == pending
        assert (tracked.resolved_at is not None) == (status == "resolved")
        now += timedelta(hours=1)


def test_acked_resolution_clears_pending_confirmation(db):
    now = datetime(2026, 10, 9, tzinfo=timezone.utc)
    finding = Finding(severity="warning", title="API", detail="Degraded",
                      kind="Deployment", resource_name="api", reason="Unavailable")
    report = HealthReport(overall_severity="warning", summary="s", findings=[finding])
    identity = fingerprint(finding)
    db.apply_diff(diff_report(report, {}, now), now)

    finding.severity = "critical"
    db.apply_diff(diff_report(report, db.load_tracked_findings(), now), now)
    tracked = db.load_tracked_findings()
    assert tracked[identity].pending_severity == "critical"
    tracked[identity].ack_until = now + timedelta(hours=12)

    report.findings = []
    diff = diff_report(report, tracked, now)
    assert not diff.should_notify()
    assert len(diff.suppressed_resolved) == 1
    db.apply_diff(diff, now)
    tracked = db.load_tracked_findings()
    assert tracked[identity].pending_severity is None
    assert tracked[identity].resolved_at == now

    report.findings = [finding]
    diff = diff_report(report, tracked, now)
    assert len(diff.new) == 1
    assert diff.new[0].pending_severity is None
