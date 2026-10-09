"""Integration tests for persistence.py against a real Postgres.

Skipped unless TEST_DATABASE_URL is set, e.g.:

    docker run --rm -d --name pg -p 55433:5432 \\
      -e POSTGRES_PASSWORD=testpw -e POSTGRES_USER=sre_agent \\
      -e POSTGRES_DB=sre_agent postgres:16-alpine
    TEST_DATABASE_URL=postgresql://sre_agent:testpw@127.0.0.1:55433/sre_agent \\
      python -m pytest tests/test_persistence.py

These cover the SQL that is easy to get subtly wrong: the finding_state upsert's
first_seen CASE expression, make_interval, = ANY(...), and the atomic counter.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from monitor_state import StoredFinding, diff_report, fingerprint
from persistence import NullDatabase, PostgresDatabase, init_persistence
from schemas import Finding, HealthReport

TEST_DSN = os.getenv("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_DATABASE_URL not set"
)

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def db():
    _ck, _st, database = init_persistence(TEST_DSN)
    if not isinstance(database, PostgresDatabase):
        pytest.skip("could not reach TEST_DATABASE_URL")
    yield database
    database.close()


@pytest.fixture(autouse=True)
def clean(db):
    """Each test starts from an empty schema."""
    with db._pool.connection() as conn:
        conn.execute(
            "TRUNCATE sessions, hitl_audit, finding_state, monitor_reports, monitor_meta"
        )
    yield


def finding(severity="critical", resource_name="api-6b474476c4-6nqxr",
            reason="CrashLoopBackOff", title="CrashLoopBackOff on api"):
    return Finding(severity=severity, title=title, detail="d", namespace="prod",
                   kind="Pod", resource_name=resource_name, reason=reason)


def report(*findings, severity="critical"):
    return HealthReport(overall_severity=severity, summary="s", findings=list(findings))


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

SESSION = {
    "id": "slack-1234.5678", "thread_id": "slack-1234.5678", "status": "interrupted",
    "source": "slack", "pending_decisions": 3,
    "pending_actions": [
        {"name": "kubectl_scale_deployment", "args": {"name": "api", "replicas": 5}}
    ],
    "interrupt_data": ["Interrupt(...)"], "last_response": "",
    "slack_message_ts": "111.222", "slack_channel": "C123", "slack_thread_ts": "333.444",
}


def test_session_round_trips_including_jsonb(db):
    db.save_session(SESSION)
    got = db.load_session(SESSION["id"])
    assert got["status"] == "interrupted"
    assert got["pending_decisions"] == 3
    assert got["pending_actions"][0]["args"]["replicas"] == 5
    assert got["slack_channel"] == "C123"


def test_saving_the_same_session_updates_in_place(db):
    db.save_session(SESSION)
    db.save_session({**SESSION, "status": "done", "last_response": "scaled"})
    got = db.load_session(SESSION["id"])
    assert got["status"] == "done"
    assert got["last_response"] == "scaled"


def test_missing_session_is_none(db):
    assert db.load_session("nope") is None


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def test_audit_rows_are_recorded_newest_first(db):
    db.record_decision(session_id="s1", decision="approve", actor="eric",
                       actor_id="U123", source="slack",
                       tool_name="kubectl_scale_deployment",
                       tool_args={"name": "api", "replicas": 5}, result="ok")
    db.record_decision(session_id="s1", decision="approve-denied", actor="rando",
                       actor_id="U999", result="actor not in SLACK_APPROVER_IDS")
    rows = db.recent_decisions(10)
    assert len(rows) == 2
    assert rows[0]["decision"] == "approve-denied"
    assert any(r["tool_args"].get("replicas") == 5 for r in rows)
    assert any(r["actor_id"] == "U123" for r in rows)


def test_recent_decisions_limit_is_clamped(db):
    db.record_decision(session_id="s1", decision="approve")
    assert len(db.recent_decisions(10**6)) == 1


# ---------------------------------------------------------------------------
# Finding state
# ---------------------------------------------------------------------------

def test_new_then_ongoing_increments_and_preserves_first_seen(db):
    f = finding()
    fp = fingerprint(f)

    first = diff_report(report(f), db.load_tracked_findings(), NOW)
    assert len(first.new) == 1
    db.apply_diff(first, NOW)

    tracked = db.load_tracked_findings()
    assert tracked[fp].times_seen == 1
    assert tracked[fp].severity == "critical"

    later = NOW + timedelta(hours=1)
    second = diff_report(report(f), tracked, later)
    assert len(second.ongoing) == 1 and not second.should_notify()
    db.apply_diff(second, later)

    tracked = db.load_tracked_findings()
    assert tracked[fp].times_seen == 2
    assert tracked[fp].first_seen == NOW  # LEAST(...) held the original


def test_escalation_updates_stored_severity(db):
    f = finding(severity="critical")
    fp = fingerprint(f)
    prior = {fp: StoredFinding(fingerprint=fp, severity="warning",
                               first_seen=NOW, times_seen=2)}
    diff = diff_report(report(f), prior, NOW)
    assert len(diff.ongoing) == 1 and not diff.should_notify()
    db.apply_diff(diff, NOW)
    tracked = db.load_tracked_findings()
    assert tracked[fp].severity == "warning"
    assert tracked[fp].pending_severity == "critical"

    later = NOW + timedelta(hours=1)
    diff = diff_report(report(f), tracked, later)
    assert len(diff.escalated) == 1 and diff.should_notify()
    db.apply_diff(diff, later)
    assert db.load_tracked_findings()[fp].severity == "critical"
    assert db.load_tracked_findings()[fp].pending_severity is None


def test_resolution_sets_resolved_at_but_retains_the_row(db):
    f = finding()
    fp = fingerprint(f)
    db.apply_diff(diff_report(report(f), {}, NOW), NOW)

    res = NOW + timedelta(hours=3)
    diff = diff_report(report(severity="ok"), db.load_tracked_findings(), res)
    assert len(diff.resolved) == 1 and diff.should_notify()
    db.apply_diff(diff, res)

    # Retained (not deleted) so a return can be detected as a flap.
    assert db.load_tracked_findings()[fp].resolved_at is not None


def test_returning_finding_resets_first_seen_and_keeps_history(db):
    f = finding()
    fp = fingerprint(f)
    db.apply_diff(diff_report(report(f), {}, NOW), NOW)
    res = NOW + timedelta(hours=1)
    db.apply_diff(diff_report(report(severity="ok"), db.load_tracked_findings(), res), res)

    back = NOW + timedelta(hours=4)
    diff = diff_report(report(f), db.load_tracked_findings(), back)
    assert len(diff.new) == 1
    assert diff.new[0].times_seen == 2      # cumulative
    db.apply_diff(diff, back)

    tracked = db.load_tracked_findings()
    assert tracked[fp].first_seen == back   # CASE reset the clock
    assert tracked[fp].resolved_at is None


# ---------------------------------------------------------------------------
# Reports and acks
# ---------------------------------------------------------------------------

def test_ack_report_suppresses_its_findings(db):
    f = finding()
    fp = fingerprint(f)
    db.apply_diff(diff_report(report(f), {}, NOW), NOW)

    report_id = db.save_report([fp])
    assert db.ack_report(report_id, 24) == 1

    tracked = db.load_tracked_findings()
    now = datetime.now(timezone.utc)
    assert tracked[fp].is_acked(now)

    diff = diff_report(report(f), tracked, now)
    assert len(diff.suppressed) == 1 and not diff.ongoing
    assert not diff.should_notify()


def test_ack_of_unknown_or_empty_report_is_a_noop(db):
    assert db.ack_report("deadbeef", 24) == 0
    assert db.ack_report(db.save_report([]), 24) == 0


def test_check_counter_increments_atomically(db):
    assert db.next_check_number() == 1
    assert db.next_check_number() == 2


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------

def test_null_database_matches_the_postgres_interface(db):
    null = NullDatabase()
    for name in (m for m in dir(db) if not m.startswith("_")):
        assert hasattr(null, name), f"NullDatabase is missing {name}"
    assert not null.available
    assert null.load_tracked_findings() == {}
    assert null.recent_decisions() == []


@pytest.mark.parametrize("dsn", ["", "postgresql://nobody:nope@127.0.0.1:1/none"])
def test_unusable_dsn_degrades_to_memory(dsn):
    checkpointer, _store, database = init_persistence(dsn)
    assert isinstance(database, NullDatabase)
    assert not database.available
    assert type(checkpointer).__name__ in ("MemorySaver", "InMemorySaver")
