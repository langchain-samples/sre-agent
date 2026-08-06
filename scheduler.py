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

from config import MONITOR_DIGEST_EVERY_N_CHECKS, MONITOR_NOTIFY_ON_RESOLVED
from monitor_state import diff_report

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
            allocatable = (n.status.allocatable or {}) if n.status else {}
            result["nodes"].append({
                "name": n.metadata.name,
                "status": status,
                "version": (n.status.node_info.kubelet_version if n.status.node_info else "?"),
                # Denominator for utilisation. Usage without capacity is a bare
                # number the model cannot turn into a percentage, which is what
                # pushes it to invent load-based causation.
                "cpu_allocatable": allocatable.get("cpu", ""),
                "memory_allocatable": allocatable.get("memory", ""),
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
                # Utilisation vs target. Without these an "AT MAX" HPA says nothing
                # about *why* it is pinned, which is the question an operator asks next.
                "current_metrics": (getattr(status, "current_metrics", None) or []) if status else [],
                "target_metrics": getattr(spec, "metrics", None) or [],
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

    # --- Node utilisation (metrics-server) ---
    # Wrapped like every other block so a metrics-server outage degrades into a
    # COLLECTION ERRORS line rather than failing the whole scheduled check.
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
        result["errors"].append(f"node metrics: {e}")

    # --- Pod utilisation (metrics-server) ---
    try:
        pod_metrics = custom_objects().list_cluster_custom_object(
            "metrics.k8s.io", "v1beta1", "pods"
        )
        for p in pod_metrics.get("items", []):
            containers = p.get("containers", [])
            # Same unit handling as tools.kubernetes_read.kubectl_top_pods: the
            # metrics API reports CPU in nanocores ("123n") and memory in KiB
            # ("456Ki"). Entries in other units are skipped rather than guessed at.
            total_cpu_n = sum(
                int(c["usage"]["cpu"].rstrip("n")) for c in containers
                if c.get("usage", {}).get("cpu", "").endswith("n")
            )
            total_mem_ki = sum(
                int(c["usage"]["memory"].rstrip("Ki")) for c in containers
                if c.get("usage", {}).get("memory", "").endswith("Ki")
            )
            key = f"{p['metadata']['namespace']}/{p['metadata']['name']}"
            result["pod_metrics"][key] = {
                "cpu_n": total_cpu_n,
                "memory_ki": total_mem_ki,
            }
    except Exception as e:
        result["errors"].append(f"pod metrics: {e}")

    return result


def _cpu_millicores(value: str):
    """Normalise a Kubernetes CPU quantity to millicores. None if unrecognised.

    The metrics API reports nanocores ("244354044n") while node allocatable uses
    millicores ("15890m") or bare cores ("16"), so the two cannot be compared
    without converting. Unknown suffixes return None rather than a guess.
    """
    v = (value or "").strip()
    if not v:
        return None
    try:
        if v.endswith("n"):
            return int(v[:-1]) / 1_000_000
        if v.endswith("u"):
            return int(v[:-1]) / 1_000
        if v.endswith("m"):
            return float(v[:-1])
        return float(v) * 1000
    except (ValueError, TypeError):
        return None


_MEM_UNITS = {"Ki": 1, "Mi": 1024, "Gi": 1024 ** 2, "Ti": 1024 ** 3}


def _memory_ki(value: str):
    """Normalise a Kubernetes memory quantity to KiB. None if unrecognised."""
    v = (value or "").strip()
    if not v:
        return None
    try:
        for suffix, factor in _MEM_UNITS.items():
            if v.endswith(suffix):
                return int(v[: -len(suffix)]) * factor
        return int(v) / 1024  # bare bytes
    except (ValueError, TypeError):
        return None


def _fmt_mem_ki(ki) -> str:
    """Render KiB as the largest sensible binary unit."""
    if ki is None:
        return "?"
    if ki >= 1024 ** 2:
        return f"{ki / 1024 ** 2:.1f}Gi"
    if ki >= 1024:
        return f"{ki / 1024:.0f}Mi"
    return f"{ki:.0f}Ki"


def _fmt_utilization(used, total, render) -> str:
    """Render 'used/total (n%)', degrading to 'used' when total is unknown."""
    if used is None:
        return "?"
    if not total:
        return render(used)
    return f"{render(used)}/{render(total)} ({used / total * 100:.0f}%)"


def _metric_value(entry, current: bool):
    """Pull (name, rendered value) out of one HPA v2 metric spec or status entry.

    HPA metrics come in resource / containerResource / pods / object / external
    shapes, each nesting its value under a different attribute. Returns None
    instead of raising when a shape is unrecognised, so an exotic custom metric
    cannot break the whole snapshot.
    """
    try:
        mtype = (getattr(entry, "type", "") or "").lower()
        block = getattr(entry, mtype, None)
        if block is None:
            return None

        if mtype in ("resource", "containerresource"):
            name = getattr(block, "name", "") or mtype
        else:
            name = getattr(getattr(block, "metric", None), "name", "") or mtype

        side = getattr(block, "current" if current else "target", None)
        if side is None:
            return None

        util = getattr(side, "average_utilization", None)
        if util is not None:
            return str(name), f"{util}%"
        avg = getattr(side, "average_value", None)
        if avg is not None:
            return str(name), _render_quantity(mtype, str(name), str(avg))
        val = getattr(side, "value", None)
        if val is not None:
            return str(name), _render_quantity(mtype, str(name), str(val))
        return None
    except Exception:
        return None


_UNVERIFIED = " (units unverified)"


def _render_quantity(mtype: str, name: str, raw: str) -> str:
    """Render a resource metric Quantity in a canonical unit.

    An HPA target may be expressed in bare cores ("1") while the current side
    arrives in millicores ("4m"), so the pair is not comparable as printed.
    Only resource/containerResource cpu and memory have a canonical unit;
    pods/object/external metrics are dimensionless and pass through.
    """
    if mtype not in ("resource", "containerresource"):
        return raw
    lname = name.lower()
    if lname == "cpu":
        v = _cpu_millicores(raw)
        return f"{v:.0f}m" if v is not None else f"{raw}{_UNVERIFIED}"
    if lname == "memory":
        v = _memory_ki(raw)
        return _fmt_mem_ki(v) if v is not None else f"{raw}{_UNVERIFIED}"
    return raw


def _format_hpa_metrics(h: dict) -> str:
    """Render ' (cpu 12%/target 80%)' for an HPA line, or '' when unavailable."""
    current, target = {}, {}
    for entry in h.get("current_metrics") or []:
        got = _metric_value(entry, current=True)
        if got:
            current[got[0]] = got[1]
    for entry in h.get("target_metrics") or []:
        got = _metric_value(entry, current=False)
        if got:
            target[got[0]] = got[1]

    if not current and not target:
        return ""

    parts = []
    for name in list(target) + [n for n in current if n not in target]:
        cur = current.get(name, "?")
        tgt = target.get(name)
        parts.append(f"{name} {cur}/target {tgt}" if tgt else f"{name} {cur}")
    return f" ({', '.join(parts)})" if parts else ""


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
    pod_metrics = data.get("pod_metrics") or {}

    def _pod_usage(namespace: str, name: str) -> str:
        """Render ' cpu=12m mem=48Mi' for a pod, or '' when metrics are absent."""
        m = pod_metrics.get(f"{namespace}/{name}")
        if not m:
            return ""
        return f"  cpu={m['cpu_n'] / 1_000_000:.0f}m mem={_fmt_mem_ki(m['memory_ki'])}"

    if data["unhealthy_pods"]:
        lines.append("\n=== UNHEALTHY PODS ===")
        for p in data["unhealthy_pods"]:
            lines.append(
                f"  {p['namespace']}/{p['name']}  {p['status']}  restarts={p['restarts']}  age={p['age']}"
                f"{_pod_usage(p['namespace'], p['name'])}"
            )
    else:
        total = len(data["pods"])
        lines.append(f"\n=== PODS === all {total} pods healthy")

    # Busiest pods. Capped at 10 so a large cluster cannot inflate the prompt,
    # and so a hot-but-healthy pod is still visible to the analysis step.
    if pod_metrics:
        top = sorted(pod_metrics.items(), key=lambda kv: kv[1]["cpu_n"], reverse=True)[:10]
        lines.append(f"\n=== TOP PODS BY CPU (top {len(top)} of {len(pod_metrics)}) ===")
        for key, m in top:
            lines.append(
                f"  {key}  cpu={m['cpu_n'] / 1_000_000:.0f}m  mem={_fmt_mem_ki(m['memory_ki'])}"
            )

    # Node utilisation, joined to allocatable capacity so the numbers are
    # answerable rather than raw counters.
    if data.get("node_metrics"):
        capacity = {n["name"]: n for n in data.get("nodes", [])}
        lines.append("\n=== NODE UTILIZATION ===")
        for m in data["node_metrics"]:
            node = capacity.get(m["name"], {})
            cpu = _fmt_utilization(
                _cpu_millicores(m.get("cpu")),
                _cpu_millicores(node.get("cpu_allocatable")),
                lambda v: f"{v:.0f}m",
            )
            mem = _fmt_utilization(
                _memory_ki(m.get("memory")),
                _memory_ki(node.get("memory_allocatable")),
                _fmt_mem_ki,
            )
            lines.append(f"  {m['name']}  cpu={cpu}  memory={mem}")

    # HPAs
    if data["hpas"]:
        lines.append("\n=== HPAs ===")
        for h in data["hpas"]:
            at_max = " ⚠ AT MAX" if h["current"] == h["max"] else ""
            lines.append(
                f"  {h['namespace']}/{h['name']}  {h['current']}/{h['max']}{at_max}"
                f"{_format_hpa_metrics(h)}"
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


_SEVERITY_SYNONYMS = {
    "none": "info", "informational": "info", "low": "info", "minor": "info",
    "medium": "warning", "moderate": "warning", "warn": "warning",
    "high": "critical", "severe": "critical", "fatal": "critical",
    "error": "critical", "urgent": "critical",
}


def _coerce_severity(value, allowed: tuple, default: str) -> str:
    """Map a model-supplied severity onto the allowed vocabulary."""
    v = str(value or "").strip().lower()
    if v in allowed:
        return v
    v = _SEVERITY_SYNONYMS.get(v, v)
    return v if v in allowed else default


def _repair_health_report(payload):
    """Salvage a HealthReport whose enums drifted. Returns None if unsalvageable.

    Without this, one out-of-vocabulary severity discards an entire hourly
    report and the operator is told to "review the cluster manually" while the
    findings the model actually produced are thrown away. Individual bad
    findings are dropped; the rest of the report survives.
    """
    from schemas import Finding, HealthReport

    if not isinstance(payload, dict):
        return None

    data = dict(payload)
    findings = []
    for raw in data.get("findings") or []:
        if not isinstance(raw, dict):
            continue
        f = dict(raw)
        f["severity"] = _coerce_severity(f.get("severity"), ("critical", "warning", "info"), "info")
        try:
            findings.append(Finding.model_validate(f))
        except Exception:
            continue  # drop only this finding
    data["findings"] = findings

    overall = _coerce_severity(
        data.get("overall_severity"), ("critical", "warning", "info", "ok"), ""
    )
    if not overall:
        seen = {f.severity for f in findings}
        overall = (
            "critical" if "critical" in seen
            else "warning" if "warning" in seen
            else "info" if seen else "ok"
        )
    data["overall_severity"] = overall
    data["summary"] = str(data.get("summary") or "").strip() or "Health check completed."

    actions = data.get("recommended_actions") or []
    data["recommended_actions"] = [str(a) for a in actions if isinstance(a, (str, int, float))]

    try:
        return HealthReport.model_validate(data)
    except Exception:
        return None


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
        # Try to salvage before giving up, so a drifted enum does not cost the
        # operator the entire report.
        repaired = _repair_health_report(tool_input)
        if repaired is not None:
            log.warning(
                "Repaired malformed HealthReport from Haiku (stop_reason=%s, severity=%s, "
                "findings=%d): %s",
                stop_reason, repaired.overall_severity, len(repaired.findings), e,
            )
            return repaired

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


def annotate_with_history(report, db):
    """Diff a report against stored state WITHOUT advancing that state.

    Used by the interactive Slack path so an on-demand check can still say
    "ongoing 6h, seen 12 times" — while leaving ``times_seen`` to mean
    "consecutive *scheduled* checks". If ad-hoc requests advanced the counters,
    a chatty channel would inflate them and the digest cadence would drift.
    """
    from monitor_state import diff_report

    return diff_report(report, db.load_tracked_findings())


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

class MonitoringScheduler:
    def __init__(self, agent, notifier, interval_minutes: int = 30, db=None):
        # agent is kept for API compatibility but is NOT used for scheduled checks
        self._agent = agent
        self._notifier = notifier
        self._interval = interval_minutes * 60
        self._task: asyncio.Task | None = None
        self._running = False
        if db is None:
            from persistence import NullDatabase

            db = NullDatabase()
        self._db = db

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
        """Synchronous: collect data + one Haiku call, then diff. Runs in thread pool."""
        try:
            report, data = run_structured_health_check()
            now = datetime.now(timezone.utc)

            # Advance state first so a Slack failure below cannot cause the next
            # run to re-report everything as new.
            check_no = self._db.next_check_number()
            diff = diff_report(report, self._db.load_tracked_findings(), now)
            self._db.apply_diff(diff, now)

            log.info(
                "Health check complete (session=%s, check=%d, severity=%s, %s, unhealthy_pods=%d)",
                session_id, check_no, report.overall_severity, diff.summary_line(),
                len(data.get("unhealthy_pods", [])),
            )

            # A digest fires every N checks so a quiet channel still proves the
            # bot is alive even when nothing has changed.
            digest_due = (
                MONITOR_DIGEST_EVERY_N_CHECKS > 0
                and check_no > 0
                and check_no % MONITOR_DIGEST_EVERY_N_CHECKS == 0
            )
            # Without a database there is no history to diff against, so fall
            # back to the old always-post behaviour rather than going silent.
            notify = (
                not self._db.available
                or diff.should_notify(MONITOR_NOTIFY_ON_RESOLVED)
                or digest_due
            )

            if not notify:
                log.info(
                    "Nothing new (%s) — suppressing Slack post (next digest at check %d)",
                    diff.summary_line(),
                    (check_no // MONITOR_DIGEST_EVERY_N_CHECKS + 1) * MONITOR_DIGEST_EVERY_N_CHECKS
                    if MONITOR_DIGEST_EVERY_N_CHECKS > 0 else -1,
                )
                return

            if self._notifier.enabled:
                report_id = self._db.save_report([d.fingerprint for d in diff.active])
                self._notifier.send_structured_report(
                    report,
                    source="scheduled digest" if digest_due and not diff.should_notify(
                        MONITOR_NOTIFY_ON_RESOLVED
                    ) else "scheduled",
                    # Without a database there is no history, so every finding
                    # would read as NEW every hour. Omit the labels rather than
                    # assert something untrue.
                    diff=diff if self._db.available else None,
                    report_id=report_id,
                )
        except Exception as e:
            log.exception("Scheduled health check failed (session=%s)", session_id)
            if self._notifier.enabled:
                self._notifier.send_alert(
                    "critical",
                    "SRE Bot — Scheduled Check Failed",
                    f"The autonomous health check encountered an error:\n```{e}```",
                )
