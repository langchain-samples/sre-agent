"""LangSmith evaluators for the sre-agent-k8s-eval dataset.

Three evaluators:
1. severity_accuracy   — Custom Code: does the response use the correct severity level?
2. tool_coverage       — Custom Code: what fraction of expected tools were called?
3. response_quality    — LLM as Judge: is the response specific, actionable, and correct?
"""
from __future__ import annotations
import re
from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI


# ---------------------------------------------------------------------------
# 1. Severity Accuracy  (Custom Code)
# ---------------------------------------------------------------------------

_SEVERITY_PATTERN = re.compile(
    r"\[(CRITICAL|WARNING|INFO|OK)\]", re.IGNORECASE
)

def _extract_severity(text: str) -> str | None:
    m = _SEVERITY_PATTERN.search(text)
    return m.group(1).upper() if m else None


def severity_accuracy(run, example):
    """Check whether the agent used the correct severity bracket.

    Looks for [CRITICAL] / [WARNING] / [INFO] / [OK] in both the expected
    and actual responses.  Returns 1 if they match, 0 otherwise.
    A missing bracket in either response is treated as a non-match.
    """
    agent_text = run["outputs"].get("expected_response", "")
    expected_text = example["outputs"].get("expected_response", "")

    actual_sev = _extract_severity(agent_text)
    expected_sev = _extract_severity(expected_text)

    if actual_sev is None or expected_sev is None:
        return {
            "severity_accuracy": 0,
            "comment": (
                f"Could not extract severity — actual='{actual_sev}', "
                f"expected='{expected_sev}'"
            ),
        }

    match = actual_sev == expected_sev
    return {
        "severity_accuracy": 1 if match else 0,
        "comment": f"actual={actual_sev}, expected={expected_sev}",
    }


# ---------------------------------------------------------------------------
# 2. Tool Coverage  (Custom Code)
# ---------------------------------------------------------------------------

def tool_coverage(run, example):
    """Fraction of expected tools that appear in the actual trajectory.

    Uses set-based overlap (order-agnostic) so partial credit is awarded.
    Returns a float in [0, 1].
    """
    actual_traj = run["outputs"].get("expected_trajectory", [])
    expected_traj = example["outputs"].get("expected_trajectory", [])

    if not expected_traj:
        return {
            "tool_coverage": 1.0,
            "comment": "No expected trajectory to check against",
        }

    actual_set = set(actual_traj)
    expected_set = set(expected_traj)
    covered = actual_set & expected_set
    score = len(covered) / len(expected_set)

    missing = expected_set - actual_set
    extra = actual_set - expected_set

    comment_parts = [f"covered {len(covered)}/{len(expected_set)} tools"]
    if missing:
        comment_parts.append(f"missing={sorted(missing)}")
    if extra:
        comment_parts.append(f"extra={sorted(extra)}")

    return {
        "tool_coverage": round(score, 3),
        "comment": ", ".join(comment_parts),
    }


# ---------------------------------------------------------------------------
# 3. Response Quality  (LLM as Judge)
# ---------------------------------------------------------------------------

class QualityGrade(BaseModel):
    reasoning: str = Field(description="Step-by-step reasoning for the score")
    score: int = Field(description="Integer score from 1 (poor) to 5 (excellent)")
    specific: bool = Field(description="True if the response names specific resources (pod names, namespaces, etc.)")
    actionable: bool = Field(description="True if the response provides clear next steps or remediation advice")
    correct_diagnosis: bool = Field(description="True if the root cause identified matches the expected response")


_judge = ChatOpenAI(model="gpt-4o-mini", temperature=0).with_structured_output(QualityGrade)

_QUALITY_PROMPT = """\
You are evaluating an SRE agent's response to a Kubernetes incident or health-check query.

## Expected response (ground truth)
{expected}

## Agent response (to evaluate)
{actual}

## Scoring rubric (1–5)
5 — Matches expected diagnosis; names specific resources; gives actionable remediation steps
4 — Correct diagnosis; mostly specific; remediation present but vague in one area
3 — Partially correct; some specifics missing; some actionable advice
2 — Diagnosis is off or too generic; little actionable content
1 — Wrong diagnosis or irrelevant response

Evaluate and return structured output."""


async def response_quality(run, example):
    """LLM-as-Judge: rate the response on specificity, actionability, and correctness."""
    agent_text = run["outputs"].get("expected_response", "")
    expected_text = example["outputs"].get("expected_response", "")

    prompt = _QUALITY_PROMPT.format(expected=expected_text, actual=agent_text)
    grade: QualityGrade = await _judge.ainvoke([{"role": "user", "content": prompt}])

    normalized = (grade["score"] - 1) / 4  # map 1-5 → 0.0-1.0

    return {
        "response_quality": round(normalized, 3),
        "comment": (
            f"score={grade['score']}/5 | "
            f"specific={grade['specific']}, actionable={grade['actionable']}, "
            f"correct_diagnosis={grade['correct_diagnosis']} | "
            f"{grade['reasoning']}"
        ),
    }
