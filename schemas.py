"""Structured output schemas shared across the SRE agent.

These replace the previous approach of emitting `[CRITICAL]`-prefixed free text
and regex-parsing it downstream. Producers (the scheduler's Haiku analysis,
evals) emit a validated `HealthReport`; consumers (Slack rendering) read typed
fields instead of parsing markdown.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

Severity = Literal["critical", "warning", "info"]
OverallSeverity = Literal["critical", "warning", "ok"]


class Finding(BaseModel):
    """A single issue or observation about the cluster."""

    severity: Severity = Field(
        description="critical = must fix now (down/crashloop/OOM); "
        "warning = fix soon (missing PDB/probes, :latest images); "
        "info = optimization opportunity (right-sizing, orphaned PVs)."
    )
    title: str = Field(description="Short headline, e.g. 'CrashLoopBackOff on api-7d9'")
    detail: str = Field(description="Specific explanation naming resources and the cause.")
    namespace: str = Field(default="", description="Kubernetes namespace, if applicable.")


class HealthReport(BaseModel):
    """A structured cluster health report."""

    overall_severity: OverallSeverity = Field(
        description="Highest severity across all findings; 'ok' if the cluster is healthy."
    )
    summary: str = Field(
        description="One- or two-sentence overall summary that names the specific "
        "resources involved and states the reason for overall_severity. Never a bare "
        "completion acknowledgement such as 'Health check completed.'"
    )
    findings: list[Finding] = Field(
        default_factory=list,
        description="All issues found, most severe first. Empty if the cluster is healthy.",
    )
    recommended_actions: list[str] = Field(
        default_factory=list,
        description="Concrete, ordered next steps. Every warning or critical finding "
        "must be covered by at least one concrete next step. May be empty only when "
        "overall_severity is 'ok'.",
    )

    @property
    def has_issues(self) -> bool:
        return self.overall_severity in ("critical", "warning")

    @model_validator(mode="after")
    def _require_actions_for_issues(self) -> "HealthReport":
        # Backstop only: a failure here falls through to the "malformed result"
        # degraded-report path in _analyse_with_haiku, which is itself low-information.
        # The field descriptions and the system prompt are the primary fix.
        if self.overall_severity in ("warning", "critical") and self.findings and not self.recommended_actions:
            raise ValueError(
                "recommended_actions must not be empty when overall_severity is "
                "'warning' or 'critical' and findings are present"
            )
        return self
