"""Main SRE orchestrator agent."""
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.memory import InMemoryStore

from config import MODEL, DEFAULT_NAMESPACES
from tools import READ_TOOLS
from subagents import ALL_SUBAGENTS

SYSTEM_PROMPT = f"""You are an autonomous SRE (Site Reliability Engineering) bot specializing in Kubernetes.

Your job is to proactively monitor, diagnose, and improve Kubernetes cluster health.

## Default Namespaces
Unless told otherwise, check these namespaces: {', '.join(DEFAULT_NAMESPACES) or 'auto-discover all non-system namespaces'}.

## How to Handle Requests

### For health audits / cluster checks:
1. Use write_todos to plan your checks
2. Run get_cluster_summary first for an overview
3. Delegate deep analysis to specialized subagents in parallel:
   - task(agent="pod-inspector") — pod health, crashes, OOM, image pull errors
   - task(agent="scaling-analyzer") — HPA, replicas, node capacity
   - task(agent="performance-analyzer") — CPU/memory right-sizing
   - task(agent="log-analyzer") — error detection in logs
   - task(agent="security-auditor") — RBAC, privileged pods, NetworkPolicies, image tags
   - task(agent="reliability-auditor") — PDBs, probes, endpoint health, single-replica SPOFs
   - task(agent="job-inspector") — failed/suspended Jobs and CronJobs
   - task(agent="config-auditor") — missing limits, orphaned PVs, selector mismatches
4. Synthesize all findings into a prioritized report using EXACTLY this structure
   (section headers must be on their own line, no extra words):
   [CRITICAL]
   • *item name* — explanation
   [WARNING]
   • *item name* — explanation
   [INFO]
   • *item name* — explanation
   Recommended actions:
   1. action
   Use *bold* (single asterisks) for emphasis — NOT **double asterisks**.
   Severity definitions:
   - CRITICAL: must fix immediately (service down, crash loops, OOM kills, 0 ready endpoints)
   - WARNING: should fix soon (no PDB, missing probes, :latest images, wildcard RBAC)
   - INFO: optimization opportunities (right-sizing, orphaned PVs, suspended CronJobs)
   Omit a section entirely if there are no findings for it.
5. Use send_slack_notification for each significant finding and a final summary

### For applying changes:
1. Only proceed after presenting findings and getting user confirmation
2. Delegate ALL changes to task(agent="change-executor") — never apply changes directly
3. The change-executor will pause for your approval before each write operation
4. After a change completes, call send_slack_notification with the result

### Slack notification guidelines:
- severity='critical' → CrashLoopBackOff, OOMKilled, deployment not ready, node NotReady,
                        service with 0 ready endpoints, privileged container, cluster-admin misconfiguration
- severity='warning'  → HPA at max replicas, resource limits too low, high restart counts,
                        missing PDB on multi-replica workload, missing probes, :latest image tags,
                        failed/stuck jobs, selector mismatch, namespace with no NetworkPolicy
- severity='info'     → audit summary, right-sizing recommendations, suspended CronJobs,
                        orphaned PVs, missing resource requests
- severity='ok'       → all clear, successful change applied

## Safety Rules
- NEVER apply changes without explicit user confirmation
- Always use change-executor subagent for any write operations (it enforces HITL)
- Prefer rollout_restart over pod deletes for graceful restarts
- For scaling changes, consider impact on node capacity first
"""


def _content_text(content: Any) -> str:
    """Extract user-visible text from a LangChain message content field."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part.strip() for part in parts if part and part.strip()).strip()
    if content is None:
        return ""
    return str(content).strip()


def _message_text(message: Any) -> str:
    """Extract user-visible text from a LangChain message or message dict."""
    if isinstance(message, dict):
        return _content_text(message.get("content"))
    return _content_text(getattr(message, "content", ""))


def _message_name(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("name") or "")
    return str(getattr(message, "name", "") or "")


def _tool_call_name(tool_call: Any) -> str:
    if isinstance(tool_call, dict):
        return str(tool_call.get("name") or "")
    return str(getattr(tool_call, "name", "") or "")


def _tool_call_args(tool_call: Any) -> dict:
    if isinstance(tool_call, dict):
        args = tool_call.get("args") or tool_call.get("input") or {}
    else:
        args = getattr(tool_call, "args", {}) or getattr(tool_call, "input", {}) or {}
    return args if isinstance(args, dict) else {}


def _tool_call_id(tool_call: Any) -> str:
    if isinstance(tool_call, dict):
        return str(tool_call.get("id") or "")
    return str(getattr(tool_call, "id", "") or "")


def _message_tool_call_id(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("tool_call_id") or "")
    return str(getattr(message, "tool_call_id", "") or "")


def _message_tool_calls(message: Any) -> list[Any]:
    calls = []
    tool_calls = []
    if isinstance(message, dict):
        tool_calls = message.get("tool_calls") or []
        content = message.get("content") or []
    else:
        tool_calls = getattr(message, "tool_calls", []) or []
        content = getattr(message, "content", []) or []
    calls.extend(tool_calls)
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                calls.append(item)
    return calls


def _change_executor_task_call_ids(messages: list[Any]) -> set[str]:
    call_ids = set()
    for message in messages:
        for call in _message_tool_calls(message):
            args = _tool_call_args(call)
            subagent = args.get("subagent_type") or args.get("agent")
            if _tool_call_name(call) == "task" and subagent == "change-executor":
                call_id = _tool_call_id(call)
                if call_id:
                    call_ids.add(call_id)
    return call_ids


def _latest_change_executor_instruction(messages: list[Any]) -> str:
    for message in reversed(messages):
        for call in reversed(_message_tool_calls(message)):
            if _tool_call_name(call) != "task":
                continue
            args = _tool_call_args(call)
            subagent = args.get("subagent_type") or args.get("agent")
            if subagent != "change-executor":
                continue
            instruction = args.get("description") or args.get("instruction") or args.get("task")
            return str(instruction).strip() if instruction else ""
    return ""


def _latest_task_result_text(messages: list[Any]) -> str:
    change_executor_call_ids = _change_executor_task_call_ids(messages)
    for message in reversed(messages):
        message_call_id = _message_tool_call_id(message)
        if _message_name(message) == "task" or message_call_id in change_executor_call_ids:
            return _message_text(message)
    return ""


def _change_executor_fallback(messages: list[Any]) -> str:
    task_result = _latest_task_result_text(messages)
    if task_result:
        return task_result

    instruction = _latest_change_executor_instruction(messages)
    if instruction:
        return (
            "The approved Kubernetes change was handed off to the change-executor and completed, "
            f"but no detailed subagent report was returned. Request: {instruction}"
        )
    return "The approved Kubernetes change completed, but no detailed subagent report was returned."


def _ensure_change_executor_response(result: dict) -> dict:
    """Append a response when change-executor returns no final text."""
    messages = result.get("messages", [])
    if not messages or _message_text(messages[-1]):
        return result
    if not _latest_change_executor_instruction(messages) and not _latest_task_result_text(messages):
        return result

    updated = dict(result)
    updated["messages"] = [*messages, AIMessage(content=_change_executor_fallback(messages))]
    return updated


class SREAgent:
    def __init__(self, agent):
        self._agent = agent

    def invoke(self, *args, **kwargs):
        result = self._agent.invoke(*args, **kwargs)
        if isinstance(result, dict):
            return _ensure_change_executor_response(result)
        return result

    def __getattr__(self, name):
        return getattr(self._agent, name)


def create_sre_agent(extra_tools: list | None = None):
    """Create and return the main SRE orchestrator agent."""
    checkpointer = MemorySaver()
    store = InMemoryStore()

    tools = READ_TOOLS + (extra_tools or [])

    agent = create_deep_agent(
        name="sre-agent",
        model=MODEL,
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        subagents=ALL_SUBAGENTS,
        backend=FilesystemBackend(root_dir=".", virtual_mode=True),
        checkpointer=checkpointer,
        store=store,
    )
    return SREAgent(agent)
