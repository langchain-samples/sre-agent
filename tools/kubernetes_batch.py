"""Batch workload (Jobs, CronJobs) Kubernetes read tools."""
from __future__ import annotations
import traceback
from datetime import datetime, timezone
from kubernetes.client.rest import ApiException
from langchain.tools import tool
from .k8s_client import batch_v1


def _safe(fn):
    try:
        return fn()
    except ApiException as e:
        return f"ERROR [{e.status}]: {e.reason}"
    except Exception:
        return f"ERROR: {traceback.format_exc(limit=3)}"


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


@tool
def kubectl_get_jobs(namespace: str = "default") -> str:
    """
    List all Jobs — shows active, succeeded, and failed pod counts, completion status,
    and age. Flags jobs with failures and jobs stuck in active state.
    Pass namespace='--all-namespaces' to see all namespaces.
    """
    def _run():
        if namespace == "--all-namespaces":
            items = batch_v1().list_job_for_all_namespaces().items
            lines = [f"{'NAMESPACE':<20} {'NAME':<40} {'STATUS':<12} {'ACTIVE':<8} {'SUCCEEDED':<11} {'FAILED':<8} AGE"]
        else:
            items = batch_v1().list_namespaced_job(namespace).items
            lines = [f"{'NAME':<40} {'STATUS':<12} {'ACTIVE':<8} {'SUCCEEDED':<11} {'FAILED':<8} AGE"]

        failed_jobs = []
        stuck_jobs = []

        for j in items:
            ns = j.metadata.namespace
            name = j.metadata.name
            s = j.status
            active = s.active or 0
            succeeded = s.succeeded or 0
            failed = s.failed or 0
            age = _age(j.metadata.creation_timestamp)

            # Determine status
            conditions = {c.type: c.status for c in (s.conditions or [])}
            if conditions.get("Complete") == "True":
                status = "Complete"
            elif conditions.get("Failed") == "True":
                status = "Failed"
                failed_jobs.append(f"  {ns}/{name}: {failed} failure(s)")
            elif active > 0:
                status = "Running"
                # Flag jobs running for >1h as potentially stuck
                if j.status.start_time:
                    running_for = (datetime.now(timezone.utc) - j.status.start_time).total_seconds()
                    if running_for > 3600:
                        status = "Running(long)"
                        stuck_jobs.append(f"  {ns}/{name}: running for {_age(j.status.start_time)}")
            else:
                status = "Unknown"

            if namespace == "--all-namespaces":
                lines.append(f"{ns:<20} {name:<40} {status:<12} {active:<8} {succeeded:<11} {failed:<8} {age}")
            else:
                lines.append(f"{name:<40} {status:<12} {active:<8} {succeeded:<11} {failed:<8} {age}")

        out = "\n".join(lines)
        if failed_jobs:
            out += "\n\n=== FAILED JOBS ===\n" + "\n".join(failed_jobs)
        if stuck_jobs:
            out += "\n\n=== POTENTIALLY STUCK JOBS (running >1h) ===\n" + "\n".join(stuck_jobs)
        if not items:
            return f"No Jobs found in namespace '{namespace}'"
        return out
    return _safe(_run)


@tool
def kubectl_get_cronjobs(namespace: str = "default") -> str:
    """
    List all CronJobs — shows schedule, last schedule time, active jobs, and suspended status.
    Flags suspended CronJobs and those that haven't run recently relative to their schedule.
    Pass namespace='--all-namespaces' to see all namespaces.
    """
    def _run():
        if namespace == "--all-namespaces":
            items = batch_v1().list_cron_job_for_all_namespaces().items
        else:
            items = batch_v1().list_namespaced_cron_job(namespace).items

        if not items:
            return f"No CronJobs found in namespace '{namespace}'"

        lines = [f"{'NAMESPACE':<20} {'NAME':<35} {'SCHEDULE':<20} {'SUSPENDED':<10} {'ACTIVE':<8} {'LAST-SCHEDULE':<15} AGE"]
        suspended = []
        failing = []

        for cj in items:
            ns = cj.metadata.namespace
            name = cj.metadata.name
            schedule = cj.spec.schedule or "?"
            is_suspended = cj.spec.suspend or False
            active = len(cj.status.active or [])
            last = _age(cj.status.last_schedule_time) + " ago" if cj.status.last_schedule_time else "never"
            age = _age(cj.metadata.creation_timestamp)

            if is_suspended:
                suspended.append(f"  {ns}/{name}")

            # Check for recent failures in last completed jobs
            last_successful = cj.status.last_successful_time
            last_scheduled = cj.status.last_schedule_time
            if last_scheduled and last_successful:
                gap = (last_scheduled - last_successful).total_seconds()
                if gap > 300:  # >5 min gap between schedule and last success
                    failing.append(f"  {ns}/{name}: last success {_age(last_successful)} ago, last schedule {_age(last_scheduled)} ago")

            lines.append(
                f"{ns:<20} {name:<35} {schedule:<20} "
                f"{'YES' if is_suspended else 'no':<10} {active:<8} {last:<15} {age}"
            )

        out = "\n".join(lines)
        if suspended:
            out += "\n\n=== SUSPENDED CRONJOBS ===\n" + "\n".join(suspended)
        if failing:
            out += "\n\n=== CRONJOBS WITH POTENTIAL FAILURES (last success older than last schedule) ===\n" + "\n".join(failing)
        return out
    return _safe(_run)
