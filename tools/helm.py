"""Helm tools — read and write operations for Helm release management."""
from __future__ import annotations
import json
import subprocess
import traceback
from langchain.tools import tool


def _helm(*args: str, timeout: int = 30) -> tuple[bool, str]:
    """Run a helm command. Returns (success, output)."""
    try:
        result = subprocess.run(
            ["helm", *args],
            capture_output=True, text=True, timeout=timeout
        )
        if result.returncode != 0:
            return False, f"helm {' '.join(args)} failed:\n{result.stderr.strip()}"
        return True, result.stdout.strip()
    except FileNotFoundError:
        return False, "ERROR: helm CLI not found. Install helm or add it to the container image."
    except subprocess.TimeoutExpired:
        return False, f"ERROR: helm command timed out after {timeout}s"
    except Exception:
        return False, f"ERROR: {traceback.format_exc(limit=3)}"


@tool
def helm_list_releases(namespace: str = "") -> str:
    """
    List all Helm releases with their chart name, chart version, app version, and status.
    Leave namespace empty to list across all namespaces.
    """
    args = ["list", "--output", "json"]
    if namespace:
        args += ["--namespace", namespace]
    else:
        args += ["--all-namespaces"]
    ok, output = _helm(*args)
    if not ok:
        return output
    try:
        releases = json.loads(output)
    except json.JSONDecodeError:
        return output or "(no releases found)"
    if not releases:
        scope = f"namespace '{namespace}'" if namespace else "any namespace"
        return f"No Helm releases found in {scope}"
    lines = ["NAME                    NAMESPACE           CHART                         CHART VERSION   APP VERSION   STATUS"]
    for r in releases:
        lines.append(
            f"{r.get('name', '?'):<24} {r.get('namespace', '?'):<19} "
            f"{r.get('chart', '?'):<30} {r.get('chart_version', '?') or '?':<15} "
            f"{r.get('app_version', '?') or '?':<13} {r.get('status', '?')}"
        )
    return "\n".join(lines)


@tool
def helm_get_release_values(release_name: str, namespace: str = "default") -> str:
    """Get the user-supplied values for a Helm release (as YAML)."""
    ok, output = _helm("get", "values", release_name, "--namespace", namespace)
    if not ok:
        return output
    return output or "(no custom values — using chart defaults)"


@tool
def helm_get_release_manifest(release_name: str, namespace: str = "default") -> str:
    """
    Get the rendered Kubernetes manifests for a Helm release.
    Useful for inspecting what resources a release manages.
    """
    ok, output = _helm("get", "manifest", release_name, "--namespace", namespace)
    return output if ok else output


@tool
def helm_search_chart_versions(chart_ref: str, repo_name: str = "") -> str:
    """
    Search for available versions of a Helm chart.
    chart_ref: chart name, e.g. 'nginx-ingress' or 'stable/nginx-ingress'
    repo_name: optional repo prefix if chart_ref is unqualified, e.g. 'bitnami'

    To see all versions, pass chart_ref as 'repo/chart-name'.
    Run helm_list_repos to see available repositories.
    """
    search_term = f"{repo_name}/{chart_ref}" if repo_name and "/" not in chart_ref else chart_ref
    ok, output = _helm("search", "repo", search_term, "--versions", "--output", "json")
    if not ok:
        return output
    try:
        results = json.loads(output)
    except json.JSONDecodeError:
        return output or f"No results for '{search_term}'"
    if not results:
        return f"No chart versions found for '{search_term}'. Ensure the repo is added."
    lines = ["NAME                                    CHART VERSION   APP VERSION   DESCRIPTION"]
    for r in results[:20]:  # cap at 20 versions
        desc = (r.get("description") or "")[:50]
        lines.append(
            f"{r.get('name', '?'):<40} {r.get('version', '?'):<15} "
            f"{r.get('app_version', '?') or '?':<13} {desc}"
        )
    if len(results) > 20:
        lines.append(f"... and {len(results) - 20} more versions")
    return "\n".join(lines)


@tool
def helm_list_repos() -> str:
    """List all configured Helm chart repositories."""
    ok, output = _helm("repo", "list", "--output", "json")
    if not ok:
        return output
    try:
        repos = json.loads(output)
    except json.JSONDecodeError:
        return output or "(no repos configured)"
    if not repos:
        return "No Helm repos configured. Add one with: helm repo add <name> <url>"
    lines = ["NAME                    URL"]
    for r in repos:
        lines.append(f"{r.get('name', '?'):<24} {r.get('url', '?')}")
    return "\n".join(lines)


@tool
def helm_check_for_updates(namespace: str = "") -> str:
    """
    Check all Helm releases for available chart updates by comparing installed
    versions against the latest in configured repos. Runs helm repo update first.
    Leave namespace empty to check all namespaces.
    """
    # Refresh repo metadata
    _helm("repo", "update", timeout=60)

    list_args = ["list", "--output", "json"]
    if namespace:
        list_args += ["--namespace", namespace]
    else:
        list_args += ["--all-namespaces"]
    ok, output = _helm(*list_args)
    if not ok:
        return output
    try:
        releases = json.loads(output)
    except json.JSONDecodeError:
        return output or "(no releases)"

    lines = ["RELEASE                 NAMESPACE           INSTALLED        LATEST           STATUS"]
    any_updates = False
    for r in releases:
        name = r.get("name", "?")
        ns = r.get("namespace", "?")
        chart = r.get("chart", "")  # e.g. "nginx-1.2.3"
        # chart field is "chartname-version", strip the version
        installed_version = r.get("chart_version") or ""
        # Search for latest
        # chart name without version: find last hyphen before a digit
        chart_name = chart
        for i in range(len(chart) - 1, -1, -1):
            if chart[i] == "-" and i + 1 < len(chart) and chart[i + 1].isdigit():
                chart_name = chart[:i]
                break
        ok2, search_out = _helm("search", "repo", chart_name, "--output", "json")
        latest_version = "unknown"
        if ok2:
            try:
                results = json.loads(search_out)
                if results:
                    latest_version = results[0].get("version", "unknown")
            except Exception:
                pass
        status = "UP TO DATE" if installed_version == latest_version else "UPDATE AVAILABLE"
        if status != "UP TO DATE":
            any_updates = True
        lines.append(
            f"{name:<24} {ns:<19} {installed_version:<16} {latest_version:<16} {status}"
        )
    if len(lines) == 1:
        return "No releases found."
    if not any_updates:
        lines.append("\nAll releases are up to date.")
    return "\n".join(lines)


# ── Write tools (all require HITL) ────────────────────────────────────────────

@tool
def helm_upgrade_release(
    release_name: str,
    chart: str,
    namespace: str = "default",
    version: str = "",
    values_yaml: str = "",
    reuse_values: bool = True,
) -> str:
    """
    Upgrade (or install) a Helm release to a new chart version.
    release_name: existing release name
    chart: chart reference, e.g. 'bitnami/nginx' or 'ingress-nginx/ingress-nginx'
    namespace: release namespace
    version: target chart version; leave empty for latest
    values_yaml: optional YAML overrides (merged with existing values if reuse_values=True)
    reuse_values: if True, preserves existing release values (recommended)
    REQUIRES HUMAN APPROVAL before execution.
    """
    import tempfile, os
    args = ["upgrade", release_name, chart, "--namespace", namespace, "--atomic", "--timeout", "5m"]
    if version:
        args += ["--version", version]
    if reuse_values:
        args += ["--reuse-values"]
    tmp_path = None
    if values_yaml.strip():
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(values_yaml)
            tmp_path = f.name
        args += ["--values", tmp_path]
    try:
        ok, output = _helm(*args, timeout=360)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
    return output if ok else output


@tool
def helm_rollback_release(
    release_name: str,
    namespace: str = "default",
    revision: int = 0,
) -> str:
    """
    Roll back a Helm release to a previous revision.
    revision=0 rolls back to the previous revision.
    Use helm_release_history to see available revisions.
    REQUIRES HUMAN APPROVAL before execution.
    """
    args = ["rollback", release_name, "--namespace", namespace, "--atomic", "--timeout", "5m"]
    if revision > 0:
        args.insert(2, str(revision))
    ok, output = _helm(*args, timeout=360)
    return output if ok else output


@tool
def helm_release_history(release_name: str, namespace: str = "default") -> str:
    """Show revision history for a Helm release — useful before rolling back."""
    ok, output = _helm("history", release_name, "--namespace", namespace, "--output", "json")
    if not ok:
        return output
    try:
        history = json.loads(output)
    except json.JSONDecodeError:
        return output
    if not history:
        return f"No history found for release '{release_name}' in {namespace}"
    lines = ["REVISION   STATUS      CHART                         DESCRIPTION"]
    for h in history:
        lines.append(
            f"{str(h.get('revision', '?')):<10} {h.get('status', '?'):<11} "
            f"{h.get('chart', '?'):<30} {(h.get('description') or '')[:60]}"
        )
    return "\n".join(lines)


@tool
def helm_add_repo(repo_name: str, repo_url: str) -> str:
    """
    Add a Helm chart repository and update the local cache.
    repo_name: short alias, e.g. 'bitnami'
    repo_url: repository URL
    REQUIRES HUMAN APPROVAL before execution.
    """
    ok, output = _helm("repo", "add", repo_name, repo_url)
    if not ok:
        return output
    _helm("repo", "update", timeout=60)
    return f"Added repo '{repo_name}' ({repo_url}) and updated cache."
