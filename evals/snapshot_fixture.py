"""Record and replay real cluster states as deterministic eval fixtures.

The prose-scenario dataset in this directory tests the interactive agent's
*reasoning*. It cannot reach the collector, which is where every production bug
so far has lived: restart_count treated as a fault, stale events about deleted
pods, missing utilization data. Those are detection failures, and detecting them
needs real cluster state, replayed.

Two design choices matter.

Relative time, not absolute. Every timestamp is stored as an offset in minutes
from capture. A fixture holding absolute timestamps would silently change
behaviour as it aged, since the classifier is recency-based, and a regression
test that drifts is worse than none.

Allowlist projection, not redaction. Only the fields the classifier and renderer
actually read are copied out of the Kubernetes objects. Nothing else is carried,
so pod annotations, env values, mounted secret names, and image references cannot
leak into a fixture by accident. These files are committed to a public repo.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

FIXTURE_DIR = Path(__file__).parent / "fixtures"
FIXTURE_VERSION = 1


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

def _minutes_ago(ts, now) -> float | None:
    if ts is None:
        return None
    return round((now - ts).total_seconds() / 60.0, 2)


def project_pod(pod, now) -> dict:
    """Copy only what _classify_pod reads. Everything else is discarded."""
    containers = []
    for cs in (pod.status.container_statuses or []):
        state = cs.state
        waiting = getattr(state, "waiting", None) if state else None
        term = getattr(state, "terminated", None) if state else None
        last = getattr(getattr(cs, "last_state", None), "terminated", None)

        def term_dict(t):
            if t is None:
                return None
            return {
                "reason": t.reason,
                "exit_code": t.exit_code,
                "min_ago": _minutes_ago(t.finished_at, now),
            }

        containers.append({
            "ready": bool(cs.ready),
            "restart_count": cs.restart_count or 0,
            "waiting_reason": waiting.reason if waiting else None,
            "terminated": term_dict(term),
            "last_terminated": term_dict(last),
        })

    return {
        "namespace": pod.metadata.namespace,
        "name": pod.metadata.name,
        "phase": pod.status.phase or "Unknown",
        "created_min_ago": _minutes_ago(pod.metadata.creation_timestamp, now),
        "containers": containers,
    }


def project_hpa_metrics(entries) -> list[dict]:
    """Flatten V2MetricSpec / V2MetricStatus objects into plain dicts.

    The live collector stores raw API objects on the hpa dicts, which are not
    JSON-serializable. Only the shape _metric_value walks is preserved.
    """
    out = []
    for e in entries or []:
        mtype = (getattr(e, "type", "") or "")
        block = getattr(e, mtype.lower(), None)
        if block is None:
            continue
        entry = {"type": mtype}
        if mtype.lower() in ("resource", "containerresource"):
            entry["name"] = getattr(block, "name", "")
        else:
            entry["name"] = getattr(getattr(block, "metric", None), "name", "")
        for side in ("current", "target"):
            s = getattr(block, side, None)
            if s is None:
                continue
            entry[side] = {
                "average_utilization": getattr(s, "average_utilization", None),
                "average_value": _stringify(getattr(s, "average_value", None)),
                "value": _stringify(getattr(s, "value", None)),
            }
        out.append(entry)
    return out


def _stringify(v):
    return None if v is None else str(v)


def normalize(data: dict, pods_raw, now: datetime | None = None) -> dict:
    """Build a JSON-safe fixture from a live _collect_cluster_data() result.

    `pods_raw` is the list of Kubernetes pod objects, needed because the
    collector's output dict has already flattened them past what the classifier
    reads. Event *messages* are dropped wholesale rather than sanitized: they are
    free text from arbitrary controllers and can embed hostnames, IPs, and
    connection strings.
    """
    now = now or datetime.now(timezone.utc)
    return {
        "fixture_version": FIXTURE_VERSION,
        "raw_pods": [project_pod(p, now) for p in pods_raw],
        "collected": {
            "nodes": [
                {k: n.get(k) for k in
                 ("name", "status", "version", "cpu_allocatable", "memory_allocatable")}
                for n in data.get("nodes", [])
            ],
            "deployments": [
                {k: d.get(k) for k in ("namespace", "name", "desired", "ready", "available")}
                for d in data.get("deployments", [])
            ],
            "hpas": [
                {
                    **{k: h.get(k) for k in ("namespace", "name", "min", "max", "current", "desired")},
                    "current_metrics": project_hpa_metrics(h.get("current_metrics")),
                    "target_metrics": project_hpa_metrics(h.get("target_metrics")),
                }
                for h in data.get("hpas", [])
            ],
            "node_metrics": [
                {k: m.get(k) for k in ("name", "cpu", "memory")}
                for m in data.get("node_metrics", [])
            ],
            "pod_metrics": data.get("pod_metrics", {}),
            "pvc_usage": data.get("pvc_usage", {}),
            "events": [
                {
                    "namespace": e.get("namespace"),
                    "object": e.get("object"),
                    "reason": e.get("reason"),
                    "count": e.get("count"),
                    "age_min": e.get("age_min"),
                    # message intentionally dropped, see docstring
                    "message": "",
                }
                for e in data.get("events", [])
            ],
            "errors": list(data.get("errors", [])),
        },
        "expected": {
            "unhealthy_pod_names": [],
            "max_overall_severity": "ok",
            "snapshot_must_contain": [],
            "snapshot_must_not_contain": [],
        },
    }


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

def redact(fixture: dict) -> dict:
    """Replace real infrastructure names with stable pseudonyms.

    Structure and relationships survive, so the fixture still exercises the
    classifier, but the public repo learns nothing about the real cluster.
    Pod name shape is preserved (base plus generated suffix) because
    monitor_state.normalize_resource_name depends on it.
    """
    f = json.loads(json.dumps(fixture))
    ns_map, app_map, node_map = {}, {}, {}

    def ns(name):
        return ns_map.setdefault(name, f"ns-{len(ns_map) + 1}") if name else name

    def pod(name):
        # Keep any trailing generated segments so suffix-stripping still applies.
        parts = name.split("-")
        base, suffix = name, ""
        for cut in (2, 1):
            if len(parts) > cut:
                cand, tail = "-".join(parts[:-cut]), "-".join(parts[-cut:])
                if all(len(t) >= 5 for t in parts[-cut:]):
                    base, suffix = cand, "-" + tail
                    break
        alias = app_map.setdefault(base, f"app-{len(app_map) + 1}")
        return alias + suffix

    def node(name):
        return node_map.setdefault(name, f"node-{len(node_map) + 1}") if name else name

    for p in f["raw_pods"]:
        p["namespace"], p["name"] = ns(p["namespace"]), pod(p["name"])
    c = f["collected"]
    for n in c["nodes"]:
        n["name"] = node(n["name"])
    for m in c["node_metrics"]:
        m["name"] = node(m["name"])
    for d in c["deployments"]:
        d["namespace"], d["name"] = ns(d["namespace"]), app_map.setdefault(d["name"], f"app-{len(app_map)+1}")
    for h in c["hpas"]:
        h["namespace"], h["name"] = ns(h["namespace"]), app_map.setdefault(h["name"], f"app-{len(app_map)+1}")
    for e in c["events"]:
        kind, _, obj = (e.get("object") or "/").partition("/")
        e["namespace"], e["object"] = ns(e["namespace"]), f"{kind}/{pod(obj) if obj else ''}"
    for key in ("pod_metrics", "pvc_usage"):
        c[key] = {
            f"{ns(k.split('/')[0])}/{pod(k.split('/', 1)[1])}": v
            for k, v in (c.get(key) or {}).items() if "/" in k
        }
    # Collection errors can embed API response bodies and hostnames.
    c["errors"] = ["<redacted collection error>" for _ in c["errors"]]
    return f


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

def _restore_pod(p: dict, now: datetime):
    def term(d):
        if d is None:
            return None
        finished = None if d.get("min_ago") is None else now - timedelta(minutes=d["min_ago"])
        return SimpleNamespace(reason=d.get("reason"), exit_code=d.get("exit_code"),
                               finished_at=finished)

    statuses = []
    for c in p.get("containers", []):
        wr = c.get("waiting_reason")
        statuses.append(SimpleNamespace(
            ready=c.get("ready", False),
            restart_count=c.get("restart_count", 0),
            state=SimpleNamespace(waiting=SimpleNamespace(reason=wr) if wr else None,
                                  terminated=term(c.get("terminated")), running=None),
            last_state=SimpleNamespace(terminated=term(c.get("last_terminated")),
                                       waiting=None, running=None),
        ))

    created = p.get("created_min_ago")
    return SimpleNamespace(
        metadata=SimpleNamespace(
            namespace=p["namespace"], name=p["name"],
            creation_timestamp=None if created is None else now - timedelta(minutes=created),
        ),
        status=SimpleNamespace(phase=p.get("phase", "Unknown"), container_statuses=statuses),
    )


def replay(fixture: dict, now: datetime | None = None) -> dict:
    """Rebuild the data dict a live collector would have produced.

    Runs the production classifier over the restored pods, mirroring the loop in
    scheduler._collect_cluster_data. Kept in step with that loop deliberately: if
    the two diverge, these fixtures stop testing the real path.
    """
    from scheduler import _age, _classify_pod, event_is_current

    now = now or datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    data = {k: v for k, v in fixture["collected"].items()}
    data["pods"], data["unhealthy_pods"] = [], []

    for proj in fixture["raw_pods"]:
        pod = _restore_pod(proj, now)
        unhealthy, status, extra = _classify_pod(pod, now)
        info = {
            "namespace": pod.metadata.namespace,
            "name": pod.metadata.name,
            "status": status,
            "restarts": sum(cs.restart_count for cs in pod.status.container_statuses),
            "age": _age(pod.metadata.creation_timestamp),
            **extra,
        }
        data["pods"].append(info)
        if unhealthy:
            data["unhealthy_pods"].append(info)

    # Fixtures store events as captured, BEFORE filtering, so the same predicate
    # the collector uses is exercised here rather than a copy of it.
    pod_keys = {f"{p['namespace']}/{p['name']}" for p in data["pods"]}
    kept = []
    for e in fixture["collected"].get("events", []):
        kind, _, obj = (e.get("object") or "/").partition("/")
        if event_is_current(kind, e.get("namespace"), obj, e.get("age_min"), pod_keys):
            kept.append(e)
    data["events"] = kept
    return data


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def load(name: str) -> dict:
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text())


def save(fixture: dict, name: str) -> Path:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_DIR / f"{name}.json"
    path.write_text(json.dumps(fixture, indent=2, sort_keys=True) + "\n")
    return path


def all_fixtures() -> list[tuple[str, dict]]:
    return sorted(
        (p.stem, json.loads(p.read_text()))
        for p in FIXTURE_DIR.glob("*.json")
    )
