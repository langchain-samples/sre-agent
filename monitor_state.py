"""Stateful monitoring: stable finding identity and run-to-run diffing.

The scheduled health check used to be stateless — it collected, analysed, and
posted, with no memory of the previous run. A cluster problem that persists for
a week was re-reported identically every interval, which is why
``MONITORING_ENABLED`` ended up set to "false".

This module turns the check into an incident tracker. Everything here is a pure
function over plain data so it can be unit-tested without a cluster or a
database; persistence lives in ``persistence.py``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

SEVERITY_RANK = {"info": 1, "warning": 2, "critical": 3}


def _rank(severity: str) -> int:
    return SEVERITY_RANK.get((severity or "").lower(), 0)


# ---------------------------------------------------------------------------
# Stable resource identity
# ---------------------------------------------------------------------------
# Kubernetes generates the random part of a pod name from a deliberately
# vowel-free alphabet (k8s.io/apimachinery/pkg/util/rand: "bcdfghjklmnpqrstvwxz"
# plus "2456789"), so a word like "redis" or "cache" can never be mistaken for a
# generated suffix. Matching that exact alphabet lets us strip the churn without
# eating meaningful name segments.
_RAND = "[bcdfghjklmnpqrstvwxz2456789]"

# Deployment pod: <base>-<replicaset-hash>-<pod-suffix>
_POD_DEPLOY = re.compile(rf"^(?P<base>.+?)-{_RAND}{{5,10}}-{_RAND}{{5}}$")
# DaemonSet / Job / bare-ReplicaSet pod: <base>-<pod-suffix>
_POD_SHORT = re.compile(rf"^(?P<base>.+?)-{_RAND}{{5}}$")


def normalize_resource_name(kind: str, name: str) -> str:
    """Strip the generated suffix from a pod name so restarts keep one identity.

    A CrashLoopBackOff pod is replaced with a new random name on every restart.
    Fingerprinting the raw name would make one ongoing incident look like an
    endless stream of new-and-resolved pairs, so pod names collapse to their
    controller-stable base:

        api-7f9d8b6c4-xk2p1  -> api
        log-shipper-4tzvn    -> log-shipper

    StatefulSet pods are already stable (``web-0``) and their ordinal is part of
    the object's identity, so they are left alone — the digits-only ordinal
    cannot match the 5-character random alphabet above. Non-pod kinds are
    returned unchanged.
    """
    if not name:
        return ""
    if (kind or "").lower() not in ("pod", "pods"):
        return name

    m = _POD_DEPLOY.match(name)
    if m:
        return m.group("base")
    m = _POD_SHORT.match(name)
    if m:
        return m.group("base")
    return name


def _slug(text: str) -> str:
    """Collapse free text to a coarse, stable-ish token (fingerprint fallback)."""
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:60]


def fingerprint(finding) -> str:
    """Derive a stable identity for a ``schemas.Finding``.

    Built from the constrained identity fields (namespace / kind / normalized
    name / reason) rather than the free-text title, which the model rewords
    between runs. Findings that carry no identity fields at all fall back to a
    slug of the title — less stable, but it never raises.
    """
    ns = (getattr(finding, "namespace", "") or "-").strip().lower()
    kind = (getattr(finding, "kind", "") or "").strip()
    name = normalize_resource_name(kind, (getattr(finding, "resource_name", "") or "").strip())
    reason = (getattr(finding, "reason", "") or "").strip()

    if not kind and not name and not reason:
        return f"{ns}/~/{_slug(getattr(finding, 'title', ''))}"
    if not kind and not name and reason:
        return f"{ns}/~/{_slug(reason)}"

    return f"{ns}/{kind.lower()}/{name.lower()}:{_slug(reason)}"


# ---------------------------------------------------------------------------
# Diff model
# ---------------------------------------------------------------------------

@dataclass
class StoredFinding:
    """A ``finding_state`` row — what we knew about a finding before this run."""

    fingerprint: str
    severity: str
    title: str = ""
    namespace: str = ""
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    times_seen: int = 0
    resolved_at: Optional[datetime] = None
    ack_until: Optional[datetime] = None

    def is_acked(self, now: datetime) -> bool:
        return self.ack_until is not None and self.ack_until > now


@dataclass
class FindingDelta:
    """A finding present in the current run, with its history attached."""

    finding: object          # schemas.Finding
    fingerprint: str
    status: str              # "new" | "ongoing" | "escalated"
    first_seen: datetime
    times_seen: int
    previous_severity: Optional[str] = None
    last_seen: Optional[datetime] = None

    @property
    def age(self) -> timedelta:
        """How long this finding has been open (0 for a brand-new one)."""
        return (self.last_seen or self.first_seen) - self.first_seen


@dataclass
class ResolvedFinding:
    """A finding we were tracking that is absent from the current run."""

    fingerprint: str
    title: str
    namespace: str
    severity: str
    first_seen: Optional[datetime]
    last_seen: Optional[datetime]


@dataclass
class ReportDiff:
    new: list[FindingDelta] = field(default_factory=list)
    escalated: list[FindingDelta] = field(default_factory=list)
    ongoing: list[FindingDelta] = field(default_factory=list)
    resolved: list[ResolvedFinding] = field(default_factory=list)
    # Present in the run but muted by an active ack. Still tracked, never posted.
    suppressed: list[FindingDelta] = field(default_factory=list)

    @property
    def active(self) -> list[FindingDelta]:
        """Everything currently wrong and not acked, most interesting first."""
        return self.new + self.escalated + self.ongoing

    def should_notify(self, notify_on_resolved: bool = True) -> bool:
        """True when this run contains something a human has not already seen."""
        if self.new or self.escalated:
            return True
        return bool(self.resolved) and notify_on_resolved

    def summary_line(self) -> str:
        parts = []
        for label, items in (
            ("new", self.new), ("escalated", self.escalated),
            ("ongoing", self.ongoing), ("resolved", self.resolved),
            ("acked", self.suppressed),
        ):
            if items:
                parts.append(f"{len(items)} {label}")
        return ", ".join(parts) or "no findings"


def _dedupe(findings: Iterable) -> dict[str, object]:
    """Collapse findings that share a fingerprint, keeping the most severe.

    One run can legitimately produce two findings for the same object (e.g. the
    model reports both the restart count and the OOM cause). Without this the
    later one would silently overwrite the earlier and ``times_seen`` would
    double-count.
    """
    best: dict[str, object] = {}
    for f in findings:
        fp = fingerprint(f)
        current = best.get(fp)
        if current is None or _rank(f.severity) > _rank(current.severity):
            best[fp] = f
    return best


def diff_report(
    report,
    stored: dict[str, StoredFinding],
    now: Optional[datetime] = None,
) -> ReportDiff:
    """Diff a ``schemas.HealthReport`` against previously stored finding state.

    ``stored`` maps fingerprint -> StoredFinding and should contain the rows we
    consider open *plus* any acked ones, so an ack survives across runs.

    A finding that was previously resolved and has come back counts as "new"
    again — a flapping deployment should re-alert — but keeps its cumulative
    ``times_seen`` so the report can show it is a repeat offender.
    """
    now = now or datetime.now(timezone.utc)
    diff = ReportDiff()

    current = _dedupe(getattr(report, "findings", []) or [])

    for fp, finding in current.items():
        prev = stored.get(fp)

        if prev is None or prev.resolved_at is not None:
            delta = FindingDelta(
                finding=finding,
                fingerprint=fp,
                status="new",
                first_seen=now,
                times_seen=(prev.times_seen + 1) if prev else 1,
                previous_severity=prev.severity if prev else None,
                last_seen=now,
            )
        else:
            escalated = _rank(finding.severity) > _rank(prev.severity)
            delta = FindingDelta(
                finding=finding,
                fingerprint=fp,
                status="escalated" if escalated else "ongoing",
                first_seen=prev.first_seen or now,
                times_seen=prev.times_seen + 1,
                previous_severity=prev.severity,
                last_seen=now,
            )

        # An active ack mutes a finding from notifications but does not stop us
        # tracking it — the history has to stay correct for when the ack lapses.
        if prev is not None and prev.is_acked(now):
            diff.suppressed.append(delta)
        elif delta.status == "new":
            diff.new.append(delta)
        elif delta.status == "escalated":
            diff.escalated.append(delta)
        else:
            diff.ongoing.append(delta)

    for fp, prev in stored.items():
        if fp in current or prev.resolved_at is not None:
            continue
        if prev.is_acked(now):
            # Silently close acked findings: the human already said "not now",
            # so telling them it fixed itself is not worth an interrupt.
            continue
        diff.resolved.append(
            ResolvedFinding(
                fingerprint=fp,
                title=prev.title,
                namespace=prev.namespace,
                severity=prev.severity,
                first_seen=prev.first_seen,
                last_seen=prev.last_seen,
            )
        )

    order = {"critical": 0, "warning": 1, "info": 2}
    for bucket in (diff.new, diff.escalated, diff.ongoing, diff.suppressed):
        bucket.sort(key=lambda d: order.get((d.finding.severity or "").lower(), 9))

    return diff


def humanize_age(delta: Optional[timedelta]) -> str:
    """Render a duration the way an SRE reads it: 45m, 6h, 3d."""
    if delta is None:
        return "unknown"
    s = int(delta.total_seconds())
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"
