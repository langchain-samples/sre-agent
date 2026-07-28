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

    Returns a dict with keys: nodes, pods, events, hpas, deployments,
    node_metrics, pod_metrics, errors.
    No LLM calls are made here.
    """
    from tools.k8s_client import core_v1, apps_v1, autoscaling_v2, custom_objects
    from kubernetes.client.rest import ApiException

    result: dict = {
        "nodes": [],
        "pods": [],
        "unhealthy_pods": [],
        "events": [],
        "hpas": [],
        "deployments": [],
        "node_metrics": [],
        "pod_metrics": {},
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

    # --- Node utilization (metrics.k8s.io) ---
    try:
        node_metrics = custom_objects().list_cluster_custom_object(
            "metrics.k8s.io", "v1beta1", "nodes"
        )
        for n in node_metrics.get("items", []):
            usage = n.get("usage", {})
            result["node_metrics"].append({
                "name": n["metadata"]["name"],
                "cpu": usage.get("cpu", "?"),
                "memory": usage.get("memory", "?"),
            })
    except Exception as e:
        result["errors"].append(f"node_metrics: {e}")

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

    # --- Pod utilization (metrics.k8s.io) ---
    try:
        pod_metrics = custom_objects().list_cluster_custom_object(
            "metrics.k8s.io", "v1beta1", "pods"
        )
        for p in pod_metrics.get("items", []):
            containers = p.get("containers", [])
            total_cpu = sum(
                int(c["usage"]["cpu"].rstrip("n")) for c in containers
                if c.get("usage", {}).get("cpu", "").endswith("n")
            )
            total_mem_ki = sum(
                int(c["usage"]["memory"].rstrip("Ki")) for c in containers
                if c.get("usage", {}).get("memory", "").endswith("Ki")
            )
            key = f"{p['metadata']['namespace']}/{p['metadata']['name']}"
            result["pod_metrics"][key] = {
                "cpu": f"{total_cpu}n",
                "memory": f"{total_mem_ki}Ki",
            }
    except Exception as e:
        result["errors"].append(f"pod_metrics: {e}")

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
                "current_metrics": (status.current_metrics if status else None),
                "target_metrics": spec.metrics,
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


def _hpa_metric_part(m, field: str) -> tuple:
    """Pull (metric name, formatted value) out of an autoscaling/v2 metric spec or status."""
    for kind in ("resource", "container_resource", "pods", "external", "object"):
        block = getattr(m, kind, None)
        if block is None:
            continue
        name = (
            getattr(block, "name", None)
            or getattr(getattr(block, "metric", None), "name", None)
            or kind
        )
        holder = getattr(block, field, None)
        if holder is None:
            return (name, None)
        util = getattr(holder, "average_utilization", None)
        if util is not None:
            return (name, f"{util}%")
        val = getattr(holder, "average_value", None) or getattr(holder, "value", None)
        if val is not None:
            return (name, str(val))
        return (name, None)
    return (None, None)


def _format_hpa_metrics(h: dict) -> str:
    """Render an HPA's current-vs-target metric, or "" when no metrics were collected."""
    current = h.get("current_metrics") or []
    target = h.get("target_metrics") or []
    if not current and not target:
        return ""
    try:
        name, cur = _hpa_metric_part(current[0], "current") if current else (None, None)
        target_name, tgt = _hpa_metric_part(target[0], "target") if target else (None, None)
    except Exception:
        return ""
    label = name or target_name
    if not label or (cur is None and tgt is None):
        return ""
    parts = [f"{label} {cur}" if cur is not None else label]
    if tgt is not None:
        parts.append(f"target {tgt}")
    return f" ({'/'.join(parts)})"


def _format_snapshot(data: dict) -> str:
    """Convert the raw cluster data dict into a compact text snapshot for the LLM."""
    lines = []

    # Nodes
    lines.append("=== NODES ===")
    for n in data["nodes"]:
        lines.append(f"  {n['name']}  {n['status']}  {n['version']}")

    # Node utilization
    if data.get("node_metrics"):
        lines.append("\n=== NODE UTILIZATION ===")
        for n in data["node_metrics"]:
            lines.append(f"  {n['name']}  cpu={n['cpu']}  memory={n['memory']}")

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
            usage = (data.get("pod_metrics") or {}).get(f"{p['namespace']}/{p['name']}")
            util = f"  cpu={usage['cpu']} memory={usage['memory']}" if usage else ""
            lines.append(
                f"  {p['namespace']}/{p['name']}  {p['status']}  restarts={p['restarts']}  age={p['age']}{util}"
            )
    else:
        total = len(data["pods"])
        lines.append(f"\n=== PODS === all {total} pods healthy")

    # HPAs
    if data["hpas"]:
        lines.append("\n=== HPAs ===")
        for h in data["hpas"]:
            at_max = " ⚠ AT MAX" if h["current"] == h["max"] else ""
            metrics = _format_hpa_metrics(h)
            lines.append(
                f"  {h['namespace']}/{h['name']}  {h['current']}/{h['max']}{at_max}{metrics}"
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
def _analyse_with_haiku(snapshot: str) -> "HealthReport":
    """Send the pre-collected snapshot to claude-haiku for analysis.

    Uses forced tool-use so the model returns a validated HealthReport rather
    than free text that has to be regex-parsed downstream.
    """
    import anthropic
    from schemas import HealthReport

    client = wrap_anthropic(anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", "")))

    system = (
        "You are a concise SRE assistant. You receive a Kubernetes cluster snapshot "
        "and produce a structured health report by calling the report_health tool. "
        "Focus on actionable issues and name specific resources. Skip healthy "
        "resources unless there is a pattern worth noting. Set overall_severity to "
        "the highest severity among your findings, or 'ok' if the cluster is healthy."
    )
    tool = {
        "name": "report_health",
        "description": "Report the structured cluster health assessment.",
        "input_schema": HealthReport.model_json_schema(),
    }

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        system=system,
        tools=[tool],
        tool_choice={"type": "tool", "name": "report_health"},
        messages=[
            {
                "role": "user",
                "content": f"Cluster snapshot collected at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}:\n\n{snapshot}",
            }
        ],
    )

    stop_reason = getattr(response, "stop_reason", None)
    tool_input = next(
        (block.input for block in response.content if getattr(block, "type", None) == "tool_use"),
        None,
    )

    if tool_input is None:
        # Forced tool_choice should guarantee a tool_use block, so its absence almost
        # always means the model ran out of output tokens before finishing the call.
        # Log the shape (block types + stop_reason — never the snapshot/secrets) so
        # this is diagnosable, and surface a clearly-labelled degraded report.
        block_types = [getattr(b, "type", "?") for b in response.content]
        log.error(
            "Haiku returned no tool_use block (stop_reason=%s, blocks=%s)",
            stop_reason, block_types,
        )
        hint = " (analysis hit the output token limit)" if stop_reason == "max_tokens" else ""
        return HealthReport(
            overall_severity="warning",
            summary=f"Health analysis did not return a structured result{hint}; review the cluster manually.",
            findings=[],
            recommended_actions=[],
        )

    try:
        return HealthReport.model_validate(tool_input)
    except Exception as e:
        log.error("Failed to validate HealthReport from Haiku (stop_reason=%s): %s", stop_reason, e)
        return HealthReport(
            overall_severity="warning",
            summary="Health analysis returned a malformed result; review the cluster manually.",
            findings=[],
            recommended_actions=[],
        )


def run_structured_health_check() -> tuple["HealthReport", dict]:
    """Run the bounded, deterministic health check and return (report, raw_data).

    This is the canonical health-check implementation shared by the scheduler
    and the interactive Slack path: zero-token data collection via the
    kubernetes client, then a *single* forced-tool Haiku call. It performs a
    fixed number of steps and therefore can never hit the agent's recursion
    limit — unlike routing a "health check" request through the full Deep
    Agents orchestrator.
    """
    data = _collect_cluster_data()
    snapshot = _format_snapshot(data)
    report = _analyse_with_haiku(snapshot)
    return report, data


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
            report, data = run_structured_health_check()

            log.info(
                "Health check complete (session=%s, severity=%s, findings=%d, unhealthy_pods=%d)",
                session_id, report.overall_severity, len(report.findings),
                len(data.get("unhealthy_pods", [])),
            )

            if self._notifier.enabled:
                self._notifier.send_structured_report(report, source="scheduled")
        except Exception as e:
            log.exception("Scheduled health check failed (session=%s)", session_id)
            if self._notifier.enabled:
                self._notifier.send_alert(
                    "critical",
                    "SRE Bot — Scheduled Check Failed",
                    f"The autonomous health check encountered an error:\n```{e}```",
                )
