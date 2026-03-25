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


@traceable(name="scheduled-health-check", run_type="llm")
def _analyse_with_haiku(snapshot: str) -> tuple[str, str]:
    """Send the pre-collected snapshot to claude-haiku for analysis.

    Returns (severity, analysis_text) where severity is one of:
    'critical', 'warning', 'ok'.
    """
    import anthropic

    client = wrap_anthropic(anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", "")))

    system = (
        "You are a concise SRE assistant. You receive a Kubernetes cluster snapshot "
        "and produce a short health report. "
        "Start your response with exactly one of: [CRITICAL], [WARNING], or [OK]. "
        "Then list findings as bullet points. Keep the total response under 400 words. "
        "Focus on actionable issues. Skip healthy resources unless there is a pattern worth noting."
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
