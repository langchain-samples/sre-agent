"""LangSmith evaluators for the sre-agent dataset.

## Output contract

Every evaluator reads the agent's result from ``run["outputs"]`` using these keys.
``evals/run_eval.py`` is what produces them; if you write another target function,
it must emit the same shape.

    {
      "response":      str,          # final agent text
      "tools_called":  list[str],    # tool names, in call order
      "health_report": dict | None,  # typed HealthReport when the bounded path ran
    }

Ground truth comes from ``example["outputs"]`` and is defined by the dataset:
``expected_tools``, ``expected_actions``, ``expected_response``.

Getting this contract wrong is not hypothetical. The previous version read the
agent's output using the *ground-truth* field name ``expected_response`` and read
tools from ``expected_trajectory``, a key the dataset does not define. The result
was an evaluator that scored a perfect agent 0/31 and another that scored a
deliberately wrong agent 1.0. Nothing caught it because no runner existed, so the
suite had never executed. See tests/test_evaluators.py, which asserts that a good
and a bad agent actually separate.

Each function below is deliberately self-contained, with its helpers defined
inline. That lets upload_online_evals.py ship them to LangSmith via
inspect.getsource instead of maintaining a second hand-copied version that drifts.
"""
from __future__ import annotations

_SEVERITIES = ("CRITICAL", "WARNING", "INFO", "OK")


# ---------------------------------------------------------------------------
# 1. Severity accuracy  (reference-based, offline)
# ---------------------------------------------------------------------------

def severity_accuracy(run, example):
    """Did the agent reach the same severity as the reference?

    Accepts three encodings, because the two code paths in this repo disagree:
    the bounded scheduler path returns a typed HealthReport with a lowercase
    ``overall_severity``, while the orchestrator emits prose. The dataset writes
    prose in the form ``CRITICAL: ...``; an earlier version of this evaluator
    matched only ``[CRITICAL]`` with brackets, which appears nowhere in the
    dataset, so it extracted nothing from either side and returned 0 every time.
    """
    import re

    severities = ("CRITICAL", "WARNING", "INFO", "OK")

    def extract(outputs):
        if not isinstance(outputs, dict):
            return None
        report = outputs.get("health_report")
        if isinstance(report, dict) and report.get("overall_severity"):
            return str(report["overall_severity"]).upper()
        text = outputs.get("response") or outputs.get("expected_response") or ""
        if not isinstance(text, str):
            return None
        # Bracketed, colon-suffixed, or bare leading token.
        m = re.search(r"\[(" + "|".join(severities) + r")\]", text, re.IGNORECASE)
        if m:
            return m.group(1).upper()
        m = re.search(r"\b(" + "|".join(severities) + r")\b\s*[:\-]", text, re.IGNORECASE)
        if m:
            return m.group(1).upper()
        m = re.match(r"\s*(" + "|".join(severities) + r")\b", text, re.IGNORECASE)
        return m.group(1).upper() if m else None

    actual = extract(run.get("outputs") or {})
    expected = extract(example.get("outputs") or {})

    if expected is None:
        return {"key": "severity_accuracy", "score": None,
                "comment": "reference has no severity; not scored"}
    if actual is None:
        return {"key": "severity_accuracy", "score": 0,
                "comment": f"agent stated no severity (expected {expected})"}
    return {"key": "severity_accuracy",
            "score": 1 if actual == expected else 0,
            "comment": f"actual={actual}, expected={expected}"}


# ---------------------------------------------------------------------------
# 2. Tool coverage  (reference-based, offline)
# ---------------------------------------------------------------------------

def tool_coverage(run, example):
    """Fraction of the expected tools the agent actually called.

    Reads ``tools_called`` from the run and ``expected_tools`` from the dataset.
    The previous version read ``expected_trajectory`` from both, a key the dataset
    never defines, so it short-circuited to 1.0 for every agent including a
    deliberately wrong one.
    """
    actual = (run.get("outputs") or {}).get("tools_called") or []
    expected = (example.get("outputs") or {}).get("expected_tools") or []

    if not expected:
        return {"key": "tool_coverage", "score": None,
                "comment": "reference lists no tools; not scored"}
    if not actual:
        return {"key": "tool_coverage", "score": 0.0,
                "comment": f"agent called no tools; expected {sorted(set(expected))}"}

    actual_set, expected_set = set(actual), set(expected)
    covered = actual_set & expected_set
    missing = sorted(expected_set - actual_set)
    extra = sorted(actual_set - expected_set)

    parts = [f"covered {len(covered)}/{len(expected_set)}"]
    if missing:
        parts.append(f"missing={missing}")
    if extra:
        parts.append(f"extra={extra}")
    return {"key": "tool_coverage",
            "score": round(len(covered) / len(expected_set), 3),
            "comment": ", ".join(parts)}


# ---------------------------------------------------------------------------
# 3. Deferred work  (REFERENCE-FREE, works on production traces)
# ---------------------------------------------------------------------------

def deferred_work(run, example=None):
    """Penalise a report that hands the question back to the operator.

    Reference-free on purpose. Online evaluators score production traces, which
    have no ground-truth example, so any metric that compares against a reference
    is structurally unable to work there. The previous online evaluators were
    hand-copied reference-based ones, which is why they recorded a constant 0 and
    a constant 1.0 against every live trace.

    This measures a failure this agent actually shipped: the scheduled report
    raised capacity questions it could not answer and returned recommended actions
    like "check pod CPU/memory metrics", because the collector gathered no
    utilization data. 1.0 means the report stood on its own.
    """
    import re

    outputs = run.get("outputs") or {}
    text = outputs.get("response") or ""
    report = outputs.get("health_report")
    if isinstance(report, dict):
        text = " ".join([
            text,
            report.get("summary") or "",
            " ".join(report.get("recommended_actions") or []),
        ])

    if not text.strip():
        return {"key": "deferred_work", "score": None, "comment": "no text to score"}

    patterns = [
        r"\bcheck\b[^.]{0,40}\b(metrics|usage|logs|depth|utilization|utilisation)\b",
        r"\breview\b[^.]{0,40}\b(metrics|queue|depth|logs|utilization|utilisation)\b",
        r"\b(manually|by hand)\b[^.]{0,30}\b(verify|check|inspect|run)\b",
        r"\brun\b\s+`?kubectl\s+top",
        r"\bmonitor\b[^.]{0,30}\bto determine\b",
        r"\bunable to determine\b",
        r"\bcould not (be )?(determine|assess|verify)\b",
    ]
    hits = [p for p in patterns if re.search(p, text, re.IGNORECASE)]
    return {"key": "deferred_work",
            "score": 0.0 if hits else 1.0,
            "comment": ("report is self-contained" if not hits
                        else f"defers to the operator ({len(hits)} phrase(s) matched)")}


# ---------------------------------------------------------------------------
# 4. Finding specificity  (REFERENCE-FREE, works on production traces)
# ---------------------------------------------------------------------------

def finding_specificity(run, example=None):
    """Fraction of findings that name a concrete Kubernetes object.

    Reference-free. A finding without kind and resource_name cannot be acted on,
    and cannot be fingerprinted for the monitoring diff either, so this doubles as
    a check that the data feeding finding identity is actually populated.
    """
    report = (run.get("outputs") or {}).get("health_report")
    if not isinstance(report, dict):
        return {"key": "finding_specificity", "score": None,
                "comment": "no structured health_report on this run"}
    findings = report.get("findings") or []
    if not findings:
        return {"key": "finding_specificity", "score": None,
                "comment": "no findings to score"}

    named = [f for f in findings
             if isinstance(f, dict) and (f.get("kind") or "").strip()
             and (f.get("resource_name") or "").strip()]
    vague = [f.get("title", "?") for f in findings if f not in named]
    return {"key": "finding_specificity",
            "score": round(len(named) / len(findings), 3),
            "comment": (f"{len(named)}/{len(findings)} name a resource"
                        + (f"; vague={vague[:3]}" if vague else ""))}


# ---------------------------------------------------------------------------
# 5. Response quality  (LLM as judge, reference-based, offline)
# ---------------------------------------------------------------------------

def response_quality(run, example):
    """Judge the response against the reference on a 1-5 rubric.

    Uses Anthropic rather than OpenAI: this repo is Anthropic-based and the
    credential already exists, so an OpenAI dependency and a second key would buy
    nothing. The previous version built ChatOpenAI(gpt-4o-mini) while
    langchain-openai was absent from requirements.txt and OPENAI_API_KEY was
    configured nowhere, then annotated the result as a Pydantic model and
    subscripted it as a dict.
    """
    import json
    import os
    import re

    import anthropic

    outputs = run.get("outputs") or {}
    agent_text = outputs.get("response") or ""
    if isinstance(outputs.get("health_report"), dict):
        agent_text = agent_text or json.dumps(outputs["health_report"])
    expected_text = (example.get("outputs") or {}).get("expected_response") or ""

    if not agent_text.strip():
        return {"key": "response_quality", "score": 0.0,
                "comment": "agent produced no response text"}
    if not expected_text.strip():
        return {"key": "response_quality", "score": None,
                "comment": "reference has no expected_response; not scored"}

    prompt = (
        "You are evaluating an SRE agent's response to a Kubernetes incident.\n\n"
        "## Expected response (ground truth)\n" + expected_text + "\n\n"
        "## Agent response\n" + agent_text + "\n\n"
        "## Rubric\n"
        "5 = matches the expected diagnosis, names specific resources, gives actionable remediation\n"
        "4 = correct diagnosis, mostly specific, remediation vague in one area\n"
        "3 = partially correct, some specifics missing\n"
        "2 = diagnosis off or too generic, little actionable content\n"
        "1 = wrong diagnosis or irrelevant\n\n"
        'Reply with JSON only: {"score": <1-5>, "specific": <bool>, '
        '"actionable": <bool>, "correct_diagnosis": <bool>, "reasoning": "<str>"}'
    )

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {"key": "response_quality", "score": 0.0,
                "comment": f"judge returned unparseable output: {text[:120]}"}

    grade = json.loads(m.group())
    score = int(grade.get("score", 1))
    return {"key": "response_quality",
            "score": round((score - 1) / 4, 3),
            "comment": (f"score={score}/5 | specific={grade.get('specific')}, "
                        f"actionable={grade.get('actionable')}, "
                        f"correct_diagnosis={grade.get('correct_diagnosis')} | "
                        f"{grade.get('reasoning', '')[:240]}")}


# Reference-based evaluators need a dataset example; reference-free ones work on
# any run and are the only kind that can meaningfully score production traces.
REFERENCE_BASED = [severity_accuracy, tool_coverage, response_quality]
REFERENCE_FREE = [deferred_work, finding_specificity]
ALL_EVALUATORS = REFERENCE_BASED + REFERENCE_FREE
