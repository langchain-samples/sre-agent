"""Structured output schemas shared across the SRE agent.

These replace the previous approach of emitting `[CRITICAL]`-prefixed free text
and regex-parsing it downstream. Producers (the scheduler's model analysis,
evals) emit a validated `HealthReport`; consumers (Slack rendering) read typed
fields instead of parsing markdown.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["critical", "warning", "info"]
# Must be a superset of Severity plus "ok". The analysis prompt asks for "the
# highest severity among your findings", so every value a Finding can take has
# to be representable here. Omitting "info" made an all-info report unanswerable
# and the whole HealthReport failed validation.
OverallSeverity = Literal["critical", "warning", "info", "ok"]


class Finding(BaseModel):
    """A single issue or observation about the cluster.

    ``kind`` / ``resource_name`` / ``reason`` exist so the monitoring loop can
    derive a *stable* identity for a finding across runs. Fingerprinting the
    free-text ``title`` does not work — the model rewords it between runs
    ("CrashLoopBackOff on api-7d9" vs "api-7d9 is crash looping"), which would
    make the same ongoing problem look new every interval. See
    ``monitor_state.fingerprint``.
    """

    severity: Severity = Field(
        description="critical = must fix now (down/crashloop/OOM); "
        "warning = fix soon (missing PDB/probes, :latest images); "
        "info = optimization opportunity (right-sizing, orphaned PVs)."
    )
    title: str = Field(description="Short headline, e.g. 'CrashLoopBackOff on api-7d9'")
    detail: str = Field(description="Specific explanation naming resources and the cause.")
    namespace: str = Field(default="", description="Kubernetes namespace, if applicable.")
    kind: str = Field(
        default="",
        description="Kubernetes kind of the affected object, capitalized and singular: "
        "Pod, Deployment, StatefulSet, DaemonSet, Node, HPA, Job, CronJob, Service, "
        "PersistentVolume, Namespace. Leave empty only for cluster-wide observations.",
    )
    resource_name: str = Field(
        default="",
        description="Name of the single affected object, e.g. 'api-7d9f8b6c4-xk2p1'. "
        "Use the exact name as it appears in the snapshot. If a finding covers several "
        "objects, emit one finding per object instead of listing them here.",
    )
    reason: str = Field(
        default="",
        description="Short stable machine-style cause in CamelCase, e.g. CrashLoopBackOff, "
        "OOMKilled, ImagePullBackOff, NotReady, HPAAtMaxReplicas, MissingResourceLimits, "
        "NoPodDisruptionBudget, LatestImageTag. Reuse the same token for the same class of "
        "problem across runs — do not invent a new wording each time.",
    )


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
