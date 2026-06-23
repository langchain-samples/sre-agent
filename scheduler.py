"""Autonomous monitoring scheduler — runs health checks on a configurable interval.

Cost-optimised design: data is collected via direct Python kubernetes-client calls
(zero LLM tokens), then a *single* claude-haiku call analyses the snapshot.
This replaces the previous approach that ran the full Deep Agents orchestrator
(~20 Sonnet calls per check) with ~1 Haiku call — roughly a 95-99% cost reduction.
"""
from __future__ import annotations
import asyncio
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from langsmith import traceable
from langsmith.wrappers import wrap_anthropic

log = logging.getLogger("sre-agent.scheduler")

# ---------------------------------------------------------------------------
# Direct data-collection helpers (no LLM, no tokens)
# ---------------------------------------------------------------------------

def _age(ts) -> str:
    if ts is None:
        return "unknown"
    now = datetime.now(timezone.utc)
    delta = now - ts
    s = int(delta.total_seconds())
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _collect_cluster_data() -> dict:
    """Collect raw cluster state using the kubernetes Python client directly.

    Returns a dict with keys: nodes, pods, events, hpas, deployments, errors.
    No LLM calls are made here.
    """
    from tools.k8s_client import core_v1, apps_v1, autoscaling_v2
    from kubernetes.client.rest import ApiException

    result: dict = {
        "nodes": [],
        "pods": [],
        "unhealthy_pods": [],
        "events": [],
        "hpas": [],
        "deployments": [],
        "errors": [],
    }

    # --- Nodes ---
    try:
        for n in core_v1().list_node().items:
            conditions = {c.type: c.status for c in (n.status.conditions or [])}
            status = "Ready" if conditions.get("Ready") == "True" else "NotReady"
            result["nodes"].append({
                "name": n.metadata.name,
                "status": status,
                "version": (n.status.node_info.kubelet_version if n.status.node_info else "?"),
            })
    except Exception as e:
        result["errors"].append(f"nodes: {e}")

    # --- Pods (all namespaces) ---
    try:
        for p in core_v1().list_pod_for_all_namespaces().items:
            restarts = sum((cs.restart_count or 0) for cs in (p.status.container_statuses or []))
            phase = p.status.phase or "Unknown"
            # Dig into waiting/terminated reason for better status
            reason = phase
            for cs in (p.status.container_statuses or []):
                if cs.state and cs.state.waiting and cs.state.waiting.reason:
                    reason = cs.state.waiting.reason
                elif cs.state and cs.state.terminated and cs.state.terminated.reason:
                    if cs.state.terminated.reason != "Completed":
                        reason = cs.state.terminated.reason

            pod_info = {
                "namespace": p.metadata.namespace,
                "name": p.metadata.name,
                "status": reason,
                "restarts": restarts,
                "age": _age(p.metadata.creation_timestamp),
            }
            result["pods"].append(pod_info)
            # Flag anything that looks unhealthy
            unhealthy_reasons = {"CrashLoopBackOff", "OOMKilled", "Error", "Evicted",
                                 "ImagePullBackOff", "ErrImagePull", "Pending"}
            if reason in unhealthy_reasons or restarts >= 5 or (
                phase not in ("Running", "Succeeded") and p.metadata.namespace != "kube-system"
            ):
                result["unhealthy_pods"].append(pod_info)
    except Exception as e:
        result["errors"].append(f"pods: {e}")

    # --- Recent warning events (last 20) ---
    try:
        ev_resp = core_v1().list_event_for_all_namespaces(
            field_selector="type=Warning"
        )
        events = sorted(
            ev_resp.items,
            key=lambda e: (e.last_timestamp or e.event_time or datetime.min.replace(tzinfo=timezone.utc)),
            reverse=True,
        )[:20]
        for e in events:
            result["events"].append({
                "namespace": e.metadata.namespace,
                "reason": e.reason,
                "message": (e.message or "")[:200],
                "object": f"{e.involved_object.kind}/{e.involved_object.name}",
                "count": e.count or 1,
            })
    except Exception as e:
        result["errors"].append(f"events: {e}")

    # --- HPAs ---
    try:
        for h in autoscaling_v2().list_horizontal_pod_autoscaler_for_all_namespaces().items:
            spec = h.spec
            status = h.status
            result["hpas"].append({
                "namespace": h.metadata.namespace,
                "name": h.metadata.name,
                "min": spec.min_replicas,
                "max": spec.max_replicas,
                "current": status.current_replicas if status else "?",
                "desired": status.desired_replicas if status else "?",
            })
    except Exception as e:
        result["errors"].append(f"hpas: {e}")

    # --- Deployments (non-system namespaces) ---
    try:
        for d in apps_v1().list_deployment_for_all_namespaces().items:
            if d.metadata.namespace in ("kube-system", "kube-public", "kube-node-lease"):
                continue
            spec_replicas = d.spec.replicas or 0
            ready = (d.status.ready_replicas or 0)
            result["deployments"].append({
                "namespace": d.metadata.namespace,
                "name": d.metadata.name,
                "desired": spec_replicas,
                "ready": ready,
                "available": (d.status.available_replicas or 0),
            })
    except Exception as e:
        result["errors"].append(f"deployments: {e}")

    return result


def _format_snapshot(data: dict) -> str:
    """Convert the raw cluster data dict into a compact text snapshot for the LLM."""
    lines = []

    # Nodes
    lines.append("=== NODES ===")
    for n in data["nodes"]:
        lines.append(f"  {n['name']}  {n['status']}  {n['version']}")

    # Deployments
    lines.append("\n=== DEPLOYMENTS ===")
    for d in data["deployments"]:
        flag = " ⚠" if d["ready"] < d["desired"] else ""
        lines.append(
            f"  {d['namespace']}/{d['name']}  desired={d['desired']} ready={d['ready']}{flag}"
        )

    # Unhealthy pods
    if data["unhealthy_pods"]:
        lines.append("\n=== UNHEALTHY PODS ===")
        for p in data["unhealthy_pods"]:
            lines.append(
                f"  {p['namespace']}/{p['name']}  {p['status']}  restarts={p['restarts']}  age={p['age']}"
            )
    else:
        total = len(data["pods"])
        lines.append(f"\n=== PODS === all {total} pods healthy")

    # HPAs
    if data["hpas"]:
        lines.append("\n=== HPAs ===")
        for h in data["hpas"]:
            at_max = " ⚠ AT MAX" if h["current"] == h["max"] else ""
            lines.append(
                f"  {h['namespace']}/{h['name']}  {h['current']}/{h['max']}{at_max}"
            )

    # Recent warning events
    if data["events"]:
        lines.append("\n=== RECENT WARNING EVENTS ===")
        for e in data["events"][:10]:
            lines.append(f"  [{e['namespace']}] {e['object']} — {e['reason']}: {e['message'][:120]}")

    # Collection errors
    if data["errors"]:
        lines.append("\n=== COLLECTION ERRORS ===")
        for err in data["errors"]:
            lines.append(f"  {err}")

    return "\n".join(lines)


_VERSION_RE = re.compile(r"\bv\d+\.\d+\.\d+(?:[-+][A-Za-z0-9][A-Za-z0-9._-]*)?\b")
_DEPLOYMENT_AGGREGATE_RE = re.compile(r"\b(\d+)\s*(?:of|/)\s*(\d+)\s+deployments?\b", re.IGNORECASE)
_FACTUAL_CLAIM_PHRASES = (
    "queue depth",
    "queue latency",
    "latency",
    "queue processing",
    "workload shedding",
    "dropped tasks",
    "sustained demand",
    "missed deployment",
    "missed deployments",
    "webhook misconfiguration",
    "decommissioning",
    "maintenance window",
    "maintenance windows",
    "resource constraints",
    "no failures detected",
    "no resource constraints detected",
)
_ADVISORY_WORDS = (
    "?",
    "verify",
    "check",
    "confirm",
    "investigate",
    "recommend",
    "consider",
    "review",
    "look for",
    "look into",
)


def _snapshot_node_versions(snapshot: str) -> set[str]:
    versions: set[str] = set()
    in_nodes = False
    for line in snapshot.splitlines():
        stripped = line.strip()
        if stripped == "=== NODES ===":
            in_nodes = True
            continue
        if stripped.startswith("=== "):
            in_nodes = False
        if in_nodes and stripped:
            parts = stripped.split()
            if len(parts) >= 3:
                versions.add(parts[-1])
    return versions


def _snapshot_deployment_counts(snapshot: str) -> tuple[int, int]:
    total = 0
    ready_at_desired = 0
    in_deployments = False
    for line in snapshot.splitlines():
        stripped = line.strip()
        if stripped == "=== DEPLOYMENTS ===":
            in_deployments = True
            continue
        if stripped.startswith("=== "):
            in_deployments = False
        if not in_deployments or not stripped:
            continue
        desired_match = re.search(r"\bdesired=(\d+)\b", stripped)
        ready_match = re.search(r"\bready=(\d+)\b", stripped)
        if desired_match and ready_match:
            total += 1
            if int(ready_match.group(1)) >= int(desired_match.group(1)):
                ready_at_desired += 1
    return total, ready_at_desired


def _is_advisory(line: str) -> bool:
    lower = line.lower()
    normalized = lower.lstrip("-•* 0123456789.)")
    return "?" in lower or any(normalized.startswith(word) for word in _ADVISORY_WORDS if word != "?")


def _has_unsupported_factual_claim(line: str) -> bool:
    lower = line.lower()
    return any(phrase in lower for phrase in _FACTUAL_CLAIM_PHRASES) and not _is_advisory(line)


def _has_invalid_version(line: str, valid_versions: set[str]) -> bool:
    return any(match.group(0) not in valid_versions for match in _VERSION_RE.finditer(line))


def _has_invalid_deployment_aggregate(line: str, total: int, ready_at_desired: int) -> bool:
    for match in _DEPLOYMENT_AGGREGATE_RE.finditer(line):
        first = int(match.group(1))
        second = int(match.group(2))
        if second != total:
            return True
        lower = line.lower()
        expected = total - ready_at_desired if any(
            phrase in lower for phrase in ("not ready", "unready", "unavailable", "below desired", "not at desired")
        ) else ready_at_desired
        if first != expected:
            return True
    return False


def _sanitize_analysis(snapshot: str, text: str) -> str:
    valid_versions = _snapshot_node_versions(snapshot)
    deployment_total, ready_at_desired = _snapshot_deployment_counts(snapshot)
    kept_lines = []
    for line in text.splitlines():
        if valid_versions and _has_invalid_version(line, valid_versions):
            continue
        if deployment_total and _has_invalid_deployment_aggregate(line, deployment_total, ready_at_desired):
            continue
        if _has_unsupported_factual_claim(line):
            continue
        kept_lines.append(line)
    return "\n".join(kept_lines).strip() or "[OK]\n- No supported findings in the provided snapshot."


@traceable(name="scheduled-health-check", run_type="llm")
def _analyse_with_haiku(snapshot: str) -> tuple[str, str]:
    """Send the pre-collected snapshot to claude-haiku for analysis.

    Returns (severity, analysis_text) where severity is one of:
    'critical', 'warning', 'ok'.
    """
    import anthropic

    client = wrap_anthropic(anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", "")))

    system = (
        "You are a concise SRE assistant. You receive ONLY a Kubernetes cluster snapshot "
        "with these fields: NODES (name, ready/status, version), DEPLOYMENTS (namespace/name, "
        "desired, ready), PODS or UNHEALTHY PODS (count, healthy status, restarts, age), HPAs "
        "(current/min/max or current/max), RECENT WARNING EVENTS, and COLLECTION ERRORS. No "
        "traffic, queue, latency, error-rate, utilization, capacity, scaling-history, operator "
        "intent, deployment-history, webhook, maintenance, or decommissioning data is provided. "
        "Start your response with exactly one of: [CRITICAL], [WARNING], or [OK]. Then list "
        "findings as bullet points. Keep the total response under 400 words. Focus on actionable "
        "issues directly observed in the snapshot. Skip healthy resources unless there is an "
        "observed pattern worth noting. Do not state speculative operational claims as facts, "
        "including queue depth/latency, queue processing, workload shedding, dropped tasks, "
        "sustained demand, missed deployments, webhook misconfiguration, decommissioning, "
        "maintenance windows, resource constraints, no failures detected, or no resource "
        "constraints detected. These topics may appear only as questions to verify or "
        "recommendations, never as observations. Copy emitted values verbatim: node version "
        "strings must exactly match a value in the snapshot, including the full suffix such as "
        "v1.30.14-eks-ecaa3a6. Derive aggregate counts by counting snapshot rows; do not estimate "
        "or round counts like X of Y deployments at desired."
    )

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        system=system,
        messages=[
            {
                "role": "user",
                "content": f"Cluster snapshot collected at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}:\n\n{snapshot}",
            }
        ],
    )

    text = response.content[0].text if response.content else ""
    text = _sanitize_analysis(snapshot, text)
    if text.startswith("[CRITICAL]"):
        severity = "critical"
    elif text.startswith("[WARNING]"):
        severity = "warning"
    else:
        severity = "ok"

    return severity, text


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

class MonitoringScheduler:
    def __init__(self, agent, notifier, interval_minutes: int = 30):
        # agent is kept for API compatibility but is NOT used for scheduled checks
        self._agent = agent
        self._notifier = notifier
        self._interval = interval_minutes * 60
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self):
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        log.info("Monitoring scheduler started (interval=%dm)", self._interval // 60)

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def trigger_now(self) -> str:
        """Trigger an immediate health check outside the schedule. Returns session_id."""
        return await self._run_check()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _loop(self):
        # Stagger the first run by 30s to let the pod fully start
        await asyncio.sleep(30)
        while self._running:
            try:
                await self._run_check()
            except Exception:
                log.exception("Scheduled health check failed")
            await asyncio.sleep(self._interval)

    async def _run_check(self) -> str:
        session_id = f"sched-{uuid.uuid4().hex[:8]}"
        log.info("Starting scheduled health check (session=%s)", session_id)

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._do_check, session_id)
        return session_id

    def _do_check(self, session_id: str):
        """Synchronous: collect data + one Haiku call. Runs in thread pool."""
        try:
            data = _collect_cluster_data()
            snapshot = _format_snapshot(data)
            severity, analysis = _analyse_with_haiku(snapshot)

            has_issues = severity in ("critical", "warning")
            log.info(
                "Health check complete (session=%s, severity=%s, unhealthy_pods=%d)",
                session_id, severity, len(data.get("unhealthy_pods", [])),
            )

            if self._notifier.enabled:
                self._notifier.send_health_report(
                    analysis, has_issues=has_issues, source="scheduled"
                )
        except Exception as e:
            log.exception("Scheduled health check failed (session=%s)", session_id)
            if self._notifier.enabled:
                self._notifier.send_alert(
                    "critical",
                    "SRE Bot — Scheduled Check Failed",
                    f"The autonomous health check encountered an error:\n```{e}```",
                )
