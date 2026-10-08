"""Both providers feed the same report validation and recovery path."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from langchain_openai import ChatOpenAI

import llm
import scheduler


def _mock_provider(monkeypatch, provider, payload, stop_reason=None):
    monkeypatch.setattr(llm, "LLM_PROVIDER", provider)
    monkeypatch.setattr(scheduler, "request_health_report", llm.request_health_report)
    calls = []
    if provider == "openai":
        def respond(request):
            calls.append(json.loads(request.content))
            content = (
                [{"type": "refusal", "refusal": "Cannot produce this report."}]
                if payload is None else
                [{"type": "output_text", "text": json.dumps(payload), "annotations": []}]
            )
            return httpx.Response(200, json={
                "id": "resp_test", "object": "response", "created_at": 0,
                "status": "incomplete" if stop_reason else "completed",
                "incomplete_details": {"reason": stop_reason} if stop_reason else None,
                "model": "gpt-5.6-luna", "error": None,
                "output": [{"id": "msg_test", "type": "message", "role": "assistant",
                            "status": "completed", "content": content}],
                "usage": None,
            })

        client = httpx.Client(transport=httpx.MockTransport(respond))
        model = ChatOpenAI(model="gpt-5.6-luna", api_key="test-placeholder",
                           use_responses_api=True, http_client=client)
        monkeypatch.setattr(llm, "get_subagent_model", lambda: model)
    else:
        import anthropic
        import langsmith.wrappers

        def create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                content=[] if payload is None else [SimpleNamespace(type="tool_use", input=payload)],
                stop_reason=stop_reason or "tool_use",
            )

        monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: SimpleNamespace(
            messages=SimpleNamespace(create=create)))
        monkeypatch.setattr(langsmith.wrappers, "wrap_anthropic", lambda client: client)
    return calls


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
@pytest.mark.parametrize("severity, expected", [("critical", "critical"), ("high", "critical")])
def test_valid_and_repairable_reports_keep_findings(monkeypatch, provider, severity, expected):
    monkeypatch.delenv("ANTHROPIC_HEALTH_REPORT_MAX_TOKENS", raising=False)
    payload = {
        "overall_severity": severity, "summary": "API pod is down.",
        "findings": [{"severity": severity, "title": "API down", "detail": "CrashLoopBackOff"}],
        "recommended_actions": ["Inspect the API pod logs."],
    }
    calls = _mock_provider(monkeypatch, provider, payload)
    report = scheduler._analyse_snapshot("test snapshot")
    assert report.overall_severity == expected
    assert report.findings[0].severity == expected
    assert report.findings[0].title == "API down"
    assert report.recommended_actions == payload["recommended_actions"]
    assert report.analysis_complete
    assert len(calls) == 1  # Repair must not consume another model call.
    if provider == "openai":
        assert calls[0]["text"]["format"]["type"] == "json_schema"
    else:
        assert calls[0]["tool_choice"] == {"type": "tool", "name": "report_health"}
        assert calls[0]["max_tokens"] == 8192


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
@pytest.mark.parametrize("payload", [None, {}, {"refusal": "No"}, {"findings": 42},
    {"findings": [{"unusable": "row"}]}, {"findings": [], "recommended_actions": 42}])
def test_refusals_and_unusable_reports_degrade_to_warning(monkeypatch, provider, payload):
    _mock_provider(monkeypatch, provider, payload)
    report = scheduler._analyse_snapshot("test snapshot")
    assert report.overall_severity == "warning"
    assert "review the cluster manually" in report.summary


def test_request_failure_does_not_log_snapshot_or_credentials(monkeypatch, caplog):
    def fail(*args):
        raise RuntimeError("sensitive-placeholder")
    monkeypatch.setattr(scheduler, "request_health_report", fail)
    report = scheduler._analyse_snapshot("sensitive-snapshot")
    assert report.overall_severity == "warning"
    assert "sensitive-placeholder" not in caplog.text
    assert "sensitive-snapshot" not in caplog.text


def test_output_token_limit_keeps_its_diagnostic(monkeypatch):
    def fail(*args):
        raise scheduler.HealthReportTokenLimitError()
    monkeypatch.setattr(scheduler, "request_health_report", fail)
    assert "output token limit" in scheduler._analyse_snapshot("snapshot").summary


@pytest.mark.parametrize("provider, stop_reason", [
    ("anthropic", "max_tokens"), ("openai", "max_output_tokens"),
])
@pytest.mark.parametrize("payload", [None, {
    "overall_severity": "critical", "summary": "Partial report.",
    "findings": [{"severity": "critical", "title": "API down", "detail": "Pod crashed."}],
}])
def test_capped_responses_are_discarded(monkeypatch, caplog, provider, stop_reason, payload):
    from schemas import HealthReport

    _mock_provider(monkeypatch, provider, payload, stop_reason=stop_reason)
    with pytest.raises(llm.HealthReportTokenLimitError):
        llm.request_health_report(HealthReport.model_json_schema(), "system", "snapshot")
    report = scheduler._analyse_snapshot("snapshot")
    assert not report.analysis_complete
    assert report.overall_severity == "warning"
    assert "analysis hit the output token limit" in report.summary
    assert report.findings == []
    assert report.recommended_actions == []
    if provider == "anthropic":
        assert f"partial_findings={1 if payload else 0}" in caplog.text


@pytest.mark.parametrize("configured, expected", [
    (None, 8192), ("12000", 12000), ("", 8192), ("invalid", 8192),
    ("0", 8192), ("-1", 8192), ("1.5", 8192),
])
def test_anthropic_output_budget(monkeypatch, configured, expected):
    if configured is None:
        monkeypatch.delenv("ANTHROPIC_HEALTH_REPORT_MAX_TOKENS", raising=False)
    else:
        monkeypatch.setenv("ANTHROPIC_HEALTH_REPORT_MAX_TOKENS", configured)
    calls = _mock_provider(monkeypatch, "anthropic", {})
    llm.request_health_report({}, "system", "snapshot")
    assert calls[0]["max_tokens"] == expected


def test_health_prompt_bounds_and_groups_findings():
    from schemas import HealthReport

    system, user = scheduler._health_prompt("snapshot")
    assert "at most 10 findings" in system
    assert "same kind in the same namespace that share a reason" in system
    assert "identifying all affected resources in detail" in system
    assert "Always include recommended_actions" in system
    assert "snapshot" in user
    schema = HealthReport.model_json_schema()
    resource_description = schema["$defs"]["Finding"]["properties"]["resource_name"]["description"]
    assert "one representative name" in resource_description
    assert "analysis_complete" not in schema["properties"]


@pytest.mark.parametrize("parsing_error", [None, ValueError("Truncated JSON")])
def test_openai_chat_completion_length_is_incomplete(monkeypatch, parsing_error):
    monkeypatch.setattr(llm, "LLM_PROVIDER", "openai")
    model = Mock()
    model.with_structured_output.return_value.invoke.return_value = {
        "raw": SimpleNamespace(response_metadata={"finish_reason": "length"}),
        "parsed": {"overall_severity": "ok", "findings": []},
        "parsing_error": parsing_error,
    }
    monkeypatch.setattr(llm, "get_subagent_model", lambda: model)
    with pytest.raises(llm.HealthReportTokenLimitError):
        llm.request_health_report({}, "system", "snapshot")


def test_openai_non_limit_parsing_errors_are_preserved(monkeypatch):
    monkeypatch.setattr(llm, "LLM_PROVIDER", "openai")
    model = Mock()
    model.with_structured_output.return_value.invoke.return_value = {
        "raw": SimpleNamespace(response_metadata={"finish_reason": "stop"}),
        "parsed": None, "parsing_error": ValueError("Invalid report"),
    }
    monkeypatch.setattr(llm, "get_subagent_model", lambda: model)
    with pytest.raises(ValueError, match="Invalid report"):
        llm.request_health_report({}, "system", "snapshot")


def test_capped_scheduled_check_posts_incomplete_without_advancing_state(monkeypatch):
    from slack_notifier import SlackNotifier

    payload = {
        "overall_severity": "critical", "summary": "Partial report.",
        "findings": [{"severity": "critical", "title": "API down", "detail": "Pod crashed."}],
    }
    _mock_provider(monkeypatch, "anthropic", payload, stop_reason="max_tokens")
    monkeypatch.setattr(scheduler, "_collect_cluster_data", lambda: {})
    monkeypatch.setattr(scheduler, "_format_snapshot", lambda data: "snapshot")
    db = Mock(available=True)
    notifier = SlackNotifier.__new__(SlackNotifier)
    notifier.channel = "#sre-alerts"
    notifier._channel_id = None
    notifier._client = Mock()
    notifier._client.chat_postMessage.return_value = {"ts": "1.2", "channel": "C123"}
    scheduler.MonitoringScheduler(None, notifier, db=db)._do_check("test-session")
    db.next_check_number.assert_not_called()
    db.load_tracked_findings.assert_not_called()
    db.apply_diff.assert_not_called()
    db.save_report.assert_not_called()
    notifier._client.chat_postMessage.assert_called_once()
    posted = json.dumps(notifier._client.chat_postMessage.call_args.kwargs)
    assert "Analysis Incomplete" in posted
    assert "output token limit" in posted
    assert "All Clear" not in posted
    assert "API down" not in posted
    report = scheduler._analyse_snapshot("snapshot")
    assert scheduler.annotate_with_history(report, db) is None
    db.load_tracked_findings.assert_not_called()
