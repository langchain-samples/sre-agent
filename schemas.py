"""Structured output schemas shared across the SRE agent.

These replace the previous approach of emitting `[CRITICAL]`-prefixed free text
and regex-parsing it downstream. Producers (the scheduler's Haiku analysis,
evals) emit a validated `HealthReport`; consumers (Slack rendering) read typed
fields instead of parsing markdown.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

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
    summary: str = Field(description="One- or two-sentence overall summary.")
    findings: list[Finding] = Field(
        default_factory=list,
        description="All issues found, most severe first. Empty if the cluster is healthy.",
    )
    recommended_actions: list[str] = Field(
        default_factory=list,
        description="Concrete, ordered next steps. May be empty.",
    )

    @property
    def has_issues(self) -> bool:
        return self.overall_severity in ("critical", "warning")
