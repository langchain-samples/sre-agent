"""Tests for the evaluators themselves.

These exist because two of the three original evaluators were decoupled from
agent behaviour and nobody noticed: one scored a perfect agent 0/31, the other
scored a deliberately wrong agent 1.0. No runner existed, so the suite had never
been executed.

The core property is separation: a good agent must score higher than a bad one.
An evaluator that returns a constant is worse than no evaluator, because it looks
like data. Every reference-based evaluator here is tested against both.
"""
from __future__ import annotations

import json

import pytest

from evals.evaluators import (
    REFERENCE_BASED,
    REFERENCE_FREE,
    deferred_work,
    finding_specificity,
    severity_accuracy,
    tool_coverage,
)

DATASET = "evals/sre-agent-k8s-eval.jsonl"


def example(expected_response="CRITICAL: api-server is CrashLoopBackOff (18 restarts).",
            expected_tools=("kubectl_describe_pod", "kubectl_get_pod_logs")):
    return {"outputs": {"expected_response": expected_response,
                        "expected_tools": list(expected_tools),
                        "expected_actions": []}}


def run(response="", tools=(), health_report=None):
    return {"outputs": {"response": response, "tools_called": list(tools),
                        "health_report": health_report}}


# ---------------------------------------------------------------------------
# The property that was missing: good and bad must separate
# ---------------------------------------------------------------------------

def test_severity_accuracy_separates_good_from_bad():
    ex = example()
    good = severity_accuracy(run("CRITICAL: pod is crashlooping"), ex)["score"]
    bad = severity_accuracy(run("INFO: everything looks fine"), ex)["score"]
    assert good == 1 and bad == 0


def test_tool_coverage_separates_good_from_bad():
    ex = example()
    good = tool_coverage(run(tools=["kubectl_describe_pod", "kubectl_get_pod_logs"]), ex)["score"]
    bad = tool_coverage(run(tools=["kubectl_get_namespaces"]), ex)["score"]
    assert good == 1.0
    assert bad == 0.0


def test_no_reference_based_evaluator_returns_a_constant():
    """Guards the exact class of bug that made this file necessary."""
    ex = example()
    good = run("CRITICAL: pod is crashlooping",
               tools=["kubectl_describe_pod", "kubectl_get_pod_logs"])
    bad = run("INFO: fine", tools=["kubectl_get_namespaces"])
    for fn in REFERENCE_BASED:
        if fn.__name__ == "response_quality":
            continue  # needs a live model call, covered separately
        g, b = fn(good, ex)["score"], fn(bad, ex)["score"]
        assert g != b, f"{fn.__name__} returns the same score for good and bad agents"
        assert g > b, f"{fn.__name__} does not rank the good agent higher"


# ---------------------------------------------------------------------------
# severity_accuracy: the format contract that was broken
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("[CRITICAL] pod down", "CRITICAL"),          # bracketed
    ("CRITICAL: pod down", "CRITICAL"),           # colon, the dataset's form
    ("WARNING - hpa at max", "WARNING"),          # dash
    ("OK: nothing to report", "OK"),
    ("  info: right-sizing available", "INFO"),   # leading, lowercase
])
def test_severity_extracted_from_every_encoding(text, expected):
    ex = {"outputs": {"expected_response": f"{expected}: reference"}}
    assert severity_accuracy(run(text), ex)["score"] == 1


def test_typed_health_report_is_preferred_over_prose():
    ex = {"outputs": {"expected_response": "WARNING: hpa pinned"}}
    r = run("some prose with no severity token",
            health_report={"overall_severity": "warning", "findings": []})
    assert severity_accuracy(r, ex)["score"] == 1


def test_agent_silence_scores_zero_not_none():
    """Saying nothing is a failure, not an unscorable run."""
    res = severity_accuracy(run("no severity here"), example())
    assert res["score"] == 0
    assert "stated no severity" in res["comment"]


def test_missing_reference_is_unscored_rather_than_zero():
    res = severity_accuracy(run("CRITICAL: x"), {"outputs": {}})
    assert res["score"] is None


def test_the_real_dataset_yields_extractable_severities():
    """Regression: the old bracket-only regex extracted nothing from any example."""
    rows = [json.loads(l) for l in open(DATASET) if l.strip()]
    scored = 0
    for row in rows:
        gold = row["outputs"]["expected_response"]
        res = severity_accuracy(run(gold), {"outputs": row["outputs"]})
        if res["score"] is not None:
            scored += 1
            assert res["score"] == 1, f"self-comparison should be perfect: {gold[:60]}"
    assert scored >= len(rows) * 0.8, (
        f"only {scored}/{len(rows)} examples had an extractable severity")


# ---------------------------------------------------------------------------
# tool_coverage: the field-name contract that was broken
# ---------------------------------------------------------------------------

def test_partial_tool_coverage_is_partial_credit():
    ex = example(expected_tools=("a", "b", "c", "d"))
    assert tool_coverage(run(tools=["a", "b"]), ex)["score"] == 0.5


def test_extra_tools_do_not_reduce_the_score_but_are_reported():
    ex = example(expected_tools=("a",))
    res = tool_coverage(run(tools=["a", "zzz"]), ex)
    assert res["score"] == 1.0
    assert "extra=['zzz']" in res["comment"]


def test_calling_no_tools_scores_zero_when_tools_were_expected():
    assert tool_coverage(run(tools=[]), example())["score"] == 0.0


def test_no_expected_tools_is_unscored():
    assert tool_coverage(run(tools=["a"]), example(expected_tools=()))["score"] is None


def test_reading_the_wrong_field_would_now_fail_loudly():
    """The old code read expected_trajectory, which the dataset never defines."""
    rows = [json.loads(l) for l in open(DATASET) if l.strip()]
    assert all("expected_trajectory" not in r["outputs"] for r in rows)
    assert all(r["outputs"].get("expected_tools") is not None for r in rows)


# ---------------------------------------------------------------------------
# Reference-free evaluators (the only kind that works on production traces)
# ---------------------------------------------------------------------------

def test_reference_free_evaluators_need_no_example():
    r = run("All pods healthy.", health_report={"overall_severity": "ok", "findings": []})
    for fn in REFERENCE_FREE:
        fn(r)  # must not raise without an example


@pytest.mark.parametrize("text", [
    "Recommended action: check pod CPU/memory metrics",
    "Review the queue depth to determine the cause",
    "Run `kubectl top pods` to see utilization",
    "Unable to determine the cause from available data",
])
def test_deferred_work_catches_punting_back_to_the_operator(text):
    """This is the failure the utilization gap actually produced."""
    assert deferred_work(run(text))["score"] == 0.0


def test_deferred_work_passes_a_self_contained_report():
    r = run("", health_report={
        "overall_severity": "warning",
        "summary": "Node utilization is low (4% and 2% CPU); headroom exists.",
        "recommended_actions": ["Increase maxReplicas on api-gateway from 1 to 4"],
    })
    assert deferred_work(r)["score"] == 1.0


def test_finding_specificity_scores_named_resources():
    r = run(health_report={"findings": [
        {"title": "a", "kind": "Pod", "resource_name": "api-1"},
        {"title": "b", "kind": "", "resource_name": ""},
    ]})
    res = finding_specificity(r)
    assert res["score"] == 0.5
    assert "1/2 name a resource" in res["comment"]


def test_finding_specificity_unscored_without_a_structured_report():
    assert finding_specificity(run("free text only"))["score"] is None
