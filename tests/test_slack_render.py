"""Block Kit rendering tests for send_structured_report.

No Slack connection and no database: a fake client captures the payload so the
diff-labelling branches (NEW / ESCALATED / ongoing / RESOLVED / acked) can be
asserted directly. `ensure_ascii=False` matters — the rendered text contains
'×' and '→', which json.dumps would otherwise escape past a substring check.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import slack_notifier
from monitor_state import StoredFinding, diff_report, fingerprint
from schemas import Finding, HealthReport

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


class FakeClient:
    def __init__(self):
        self.posted = None

    def chat_postMessage(self, **kwargs):
        self.posted = kwargs
        return {"ts": "1.2", "channel": "C123"}


@pytest.fixture
def notifier():
    """A SlackNotifier with its transport replaced (bypasses __init__)."""
    n = slack_notifier.SlackNotifier.__new__(slack_notifier.SlackNotifier)
    n.channel = "#sre-alerts"
    n._channel_id = None
    n._client = FakeClient()
    return n


def finding(severity="critical", resource_name="api-6b474476c4-6nqxr",
            reason="CrashLoopBackOff", title="CrashLoopBackOff on api",
            namespace="prod", detail="container exits 1"):
    return Finding(severity=severity, title=title, detail=detail,
                   namespace=namespace, kind="Pod", resource_name=resource_name,
                   reason=reason)


def report(*findings, severity="critical"):
    return HealthReport(overall_severity=severity, summary="Cluster summary.",
                        findings=list(findings), recommended_actions=["Fix it"])


def stored(fp, severity="critical", ack_until=None, times_seen=11):
    return StoredFinding(fingerprint=fp, severity=severity, title="old title",
                         namespace="prod", first_seen=NOW - timedelta(hours=6),
                         last_seen=NOW, times_seen=times_seen, ack_until=ack_until)


def body(notifier) -> str:
    return json.dumps(notifier._client.posted, ensure_ascii=False)


# ---------------------------------------------------------------------------

def test_without_a_diff_it_renders_the_plain_report(notifier):
    """The pre-existing call signature must keep working."""
    notifier.send_structured_report(report(finding()), source="scheduled")
    payload = notifier._client.posted
    assert payload is not None
    assert payload["blocks"][0]["type"] == "header"
    assert "CrashLoopBackOff on api" in body(notifier)
    assert "*NEW*" not in body(notifier)
    assert "sre_ack" not in body(notifier)


def test_normalized_analysis_headlines_critical_without_database(notifier, monkeypatch):
    import scheduler

    payload = report(finding(), severity="warning").model_dump()
    monkeypatch.setattr(scheduler, "request_health_report", lambda *args: payload)
    normalized = scheduler._analyse_snapshot("test snapshot")
    notifier.send_structured_report(normalized, source="scheduled")
    assert "Critical" in notifier._client.posted["text"]
    assert " — Issues Found" not in notifier._client.posted["text"]


def test_new_finding_is_labelled_and_gets_an_ack_button(notifier):
    diff = diff_report(report(finding()), {}, NOW)
    notifier.send_structured_report(
        report(finding()), source="scheduled", diff=diff, report_id="abc123"
    )
    text = body(notifier)
    assert "*NEW*" in text
    assert ":red_circle:" in notifier._client.posted["text"]
    assert "sre_ack" in text
    assert '"value": "abc123"' in text
    assert "1 new" in text


def test_ongoing_finding_shows_age_and_occurrence_count(notifier):
    f = finding()
    fp = fingerprint(f)
    diff = diff_report(report(f), {fp: stored(fp)}, NOW)
    notifier.send_structured_report(report(f), source="scheduled", diff=diff)
    text = body(notifier)
    assert "ongoing" in text
    assert "6h" in text
    assert "12×" in text     # 11 stored + this run
    assert "*NEW*" not in text


def test_escalation_shows_the_severity_transition(notifier):
    f = finding(severity="critical")
    fp = fingerprint(f)
    prev = stored(fp, severity="warning")
    prev.pending_severity = "critical"
    diff = diff_report(report(f), {fp: prev}, NOW)
    notifier.send_structured_report(report(f), source="scheduled", diff=diff)
    text = body(notifier)
    assert "*ESCALATED*" in text
    assert "warning→critical" in text


def test_returning_finding_is_marked_as_a_repeat(notifier):
    f = finding()
    fp = fingerprint(f)
    prev = stored(fp)
    prev.resolved_at = NOW - timedelta(hours=1)
    diff = diff_report(report(f), {fp: prev}, NOW)
    notifier.send_structured_report(report(f), source="scheduled", diff=diff)
    text = body(notifier)
    assert "*NEW*" in text
    assert "returned" in text


def test_resolved_only_report_reads_as_recovery(notifier):
    gone = "prod/pod/api:crashloopbackoff"
    diff = diff_report(report(severity="ok"), {gone: stored(gone)}, NOW)
    notifier.send_structured_report(
        report(severity="ok"), source="scheduled", diff=diff, report_id="r5"
    )
    payload = notifier._client.posted
    assert ":large_green_circle:" in payload["text"]
    assert "Recovered" in payload["text"]
    assert "RESOLVED SINCE LAST CHECK" in body(notifier)
    # Nothing active means nothing to ack.
    assert "sre_ack" not in body(notifier)


def test_fully_acked_report_is_not_headlined_critical(notifier):
    """An acked critical must not keep screaming red at the channel."""
    f = finding()
    fp = fingerprint(f)
    diff = diff_report(
        report(f), {fp: stored(fp, ack_until=NOW + timedelta(hours=5))}, NOW
    )
    assert diff.suppressed and not diff.active
    notifier.send_structured_report(report(f), source="scheduled", diff=diff, report_id="r6")
    assert ":red_circle:" not in notifier._client.posted["text"]
    assert "acked (hidden)" in body(notifier)
    assert "sre_ack" not in body(notifier)


def test_severity_groups_are_ordered_and_complete(notifier):
    findings = [
        finding(severity="critical", resource_name="a-4tzvn", reason="R1", title="crit thing"),
        finding(severity="warning", resource_name="b-4tzvn", reason="R2", title="warn thing"),
        finding(severity="info", resource_name="c-4tzvn", reason="R3", title="info thing"),
    ]
    diff = diff_report(report(*findings), {}, NOW)
    notifier.send_structured_report(report(*findings), source="scheduled", diff=diff)
    text = body(notifier)
    assert all(t in text for t in ("crit thing", "warn thing", "info thing"))
    assert text.index("CRITICAL") < text.index("WARNING") < text.index("INFO")
    assert "Recommended Actions" in text


def test_long_detail_is_truncated_within_slack_block_limits(notifier):
    f = finding(title="huge", reason="R9", resource_name="z-4tzvn", detail="x" * 9000)
    diff = diff_report(report(f), {}, NOW)
    notifier.send_structured_report(report(f), source="scheduled", diff=diff)
    assert "(truncated)" in body(notifier)
    for att in notifier._client.posted["attachments"]:
        for blk in att.get("blocks", []):
            assert len(blk.get("text", {}).get("text", "")) <= 3000


def test_disabled_notifier_posts_nothing(notifier):
    notifier._client = None  # `enabled` is False without a client
    assert slack_notifier.SlackNotifier.send_structured_report(
        notifier, report(finding())
    ) is None
