"""Main SRE orchestrator agent."""
import logging

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
    wrap_model_call,
    wrap_tool_call,
)

from config import (
    DATABASE_URL,
    TOOL_OUTPUT_MAX_CHARS,
    DEFAULT_NAMESPACES,
    LLM_PROVIDER,
    PROMPT_CACHING,
    MODEL_CALL_RUN_LIMIT,
    MODEL_CALL_THREAD_LIMIT,
    TOOL_CALL_RUN_LIMIT,
    FS_TOOL_RUN_LIMIT,
)
from llm import get_main_model
from tools import READ_TOOLS
from subagents import ALL_SUBAGENTS

log = logging.getLogger("sre-agent.agent")

# Read-heavy filesystem tools that caused the original runaway-loop cost
# incident (agent grep/read_file-ing files in a cycle). Capped tightly below.
_FS_READ_TOOLS = ("grep", "read_file", "ls", "glob")


@wrap_model_call
def anthropic_prompt_caching(request, handler):
    """Enable Anthropic prompt caching for every model call.

    Injects ``cache_control={"type": "ephemeral"}`` into the model settings so
    langchain-anthropic places a cache breakpoint on the last message block.
    Caching is cumulative from the start of the prompt, so this caches the large
    static system prompt + tool definitions + prior turns — the parts re-sent on
    every iteration of the agent loop — instead of re-billing them each call.
    """
    settings = {**(request.model_settings or {}), "cache_control": {"type": "ephemeral"}}
    return handler(request.override(model_settings=settings))


@wrap_tool_call
def truncate_tool_output(request, handler):
    """Bound how much any single tool result can add to the message history.

    Every other guard in this file counts calls. None of them bounded bytes, and
    that combination is what produced a 632,740-token prompt against a 200,000
    ceiling with every limit satisfied: TOOL_CALL_RUN_LIMIT of 80 multiplied by
    roughly 8k tokens of output each is 640k.

    Summarization does not cover this. deepagents triggers it at 170k tokens, so it
    has 30k of headroom, while a single parallel fan-out step was observed adding
    about 580k. Summarization runs *between* steps and cannot prevent one step from
    overshooting. Capping each result is what actually bounds per-step growth.

    The marker matters: the model is told explicitly that content was elided and
    how much, so it can narrow its next query instead of assuming it saw
    everything. Silently dropping the tail would be worse than the overflow.
    """
    result = handler(request)
    content = getattr(result, "content", None)
    if not isinstance(content, str) or len(content) <= TOOL_OUTPUT_MAX_CHARS:
        return result

    name = getattr(request, "tool_name", None) or getattr(
        getattr(request, "tool", None), "name", "tool")
    dropped = len(content) - TOOL_OUTPUT_MAX_CHARS
    truncated = (
        content[:TOOL_OUTPUT_MAX_CHARS]
        + f"\n\n[TRUNCATED: {name} returned {len(content):,} characters; "
          f"{dropped:,} were dropped to protect the context window. "
          f"Narrow the request (fewer lines, one namespace, a single resource) "
          f"if you need the rest.]"
    )
    log.warning(
        "Truncated %s output: %d chars -> %d (dropped %d)",
        name, len(content), TOOL_OUTPUT_MAX_CHARS, dropped,
    )
    try:
        return result.model_copy(update={"content": truncated})
    except AttributeError:
        result.content = truncated
        return result


def _build_middleware() -> list:
    """Middleware stack: prompt caching + hard runaway-loop / cost limits."""
    middleware: list = []

    # First in the list: bound per-tool-result size before anything else sees it.
    middleware.append(truncate_tool_output)

    if PROMPT_CACHING and LLM_PROVIDER == "anthropic":
        middleware.append(anthropic_prompt_caching)

    # Backstop against runaway model spend (per-run and per-thread).
    middleware.append(
        ModelCallLimitMiddleware(
            run_limit=MODEL_CALL_RUN_LIMIT,
            thread_limit=MODEL_CALL_THREAD_LIMIT,
            exit_behavior="end",
        )
    )

    # Global tool-call cap per run.
    middleware.append(
        ToolCallLimitMiddleware(run_limit=TOOL_CALL_RUN_LIMIT, exit_behavior="end")
    )

    # Tighter per-tool caps on the read-heavy filesystem tools. exit_behavior
    # "continue" blocks the over-limit tool but lets the agent keep going and
    # summarise what it has, rather than killing the whole run.
    for tool_name in _FS_READ_TOOLS:
        middleware.append(
            ToolCallLimitMiddleware(
                tool_name=tool_name,
                run_limit=FS_TOOL_RUN_LIMIT,
                exit_behavior="continue",
            )
        )

    return middleware

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


def create_sre_agent(
    extra_tools: list | None = None,
    checkpointer=None,
    store=None,
):
    """Create and return the main SRE orchestrator agent.

    ``checkpointer`` and ``store`` are injected by ``api.py`` so the web process
    shares one Postgres pool with the session table and audit log. When omitted
    (e.g. ``python main.py``) they are built on demand — Postgres if
    ``DATABASE_URL`` is set, in-memory otherwise.

    The checkpointer is what makes HITL work at all: without it, a subagent
    interrupt has nowhere to persist, and with only an in-memory one a restart
    strands every pending approval.
    """
    if checkpointer is None or store is None:
        from persistence import init_persistence

        default_checkpointer, default_store, _db = init_persistence(DATABASE_URL)
        checkpointer = checkpointer or default_checkpointer
        store = store or default_store

    tools = READ_TOOLS + (extra_tools or [])

    agent = create_deep_agent(
        name="sre-agent",
        model=get_main_model(),
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        subagents=ALL_SUBAGENTS,
        backend=FilesystemBackend(root_dir=".", virtual_mode=True),
        middleware=_build_middleware(),
        checkpointer=checkpointer,
        store=store,
    )
    return agent
