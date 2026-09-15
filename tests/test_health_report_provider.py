"""Both providers feed the same report validation and recovery path."""
import json
from types import SimpleNamespace

import httpx
import pytest
from langchain_openai import ChatOpenAI

import llm
import scheduler


def _mock_provider(monkeypatch, provider, payload):
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
                "status": "completed", "model": "gpt-5.6-luna", "error": None,
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
                stop_reason="end_turn",
            )

        monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: SimpleNamespace(
            messages=SimpleNamespace(create=create)))
        monkeypatch.setattr(langsmith.wrappers, "wrap_anthropic", lambda client: client)
    return calls


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
@pytest.mark.parametrize("severity, expected", [("critical", "critical"), ("high", "critical")])
def test_valid_and_repairable_reports_keep_findings(monkeypatch, provider, severity, expected):
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
    assert len(calls) == 1  # Repair must not consume another model call.
    if provider == "openai":
        assert calls[0]["text"]["format"]["type"] == "json_schema"
    else:
        assert calls[0]["tool_choice"] == {"type": "tool", "name": "report_health"}
        assert calls[0]["max_tokens"] == 4096


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
