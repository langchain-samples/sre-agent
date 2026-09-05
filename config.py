import os
from dotenv import load_dotenv

load_dotenv()

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").strip().lower()
if LLM_PROVIDER not in {"anthropic", "openai"}:
    raise ValueError("LLM_PROVIDER must be either 'anthropic' or 'openai'")

# Keep the existing Anthropic defaults, while allowing every active model role
# to be overridden independently. The OpenAI defaults preserve the same tiered
# design: Sol for orchestration and Luna for read-heavy workers / extraction.
if LLM_PROVIDER == "openai":
    MODEL_ID = os.getenv("OPENAI_MODEL", "gpt-5.6-sol")
    SUBAGENT_MODEL_ID = os.getenv("OPENAI_SUBAGENT_MODEL", "gpt-5.6-luna")
else:
    MODEL_ID = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    SUBAGENT_MODEL_ID = os.getenv(
        "ANTHROPIC_SUBAGENT_MODEL", "claude-haiku-4-5-20251001"
    )

# Provider-qualified strings are understood by Deep Agents / LangChain. OpenAI
# models are instantiated explicitly in llm.py so tool-using calls use the
# Responses API; these strings remain useful for logs and configuration tests.
MODEL = f"{LLM_PROVIDER}:{MODEL_ID}"
SUBAGENT_MODEL = f"{LLM_PROVIDER}:{SUBAGENT_MODEL_ID}"
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "")

# In-cluster detection: Kubernetes injects SERVICE_ACCOUNT_TOKEN at this path
IN_CLUSTER = os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token")

# For local dev: optionally pin to a specific context / namespace list
K8S_CONTEXT = os.getenv("K8S_CONTEXT", "")
DEFAULT_NAMESPACES = [
    ns.strip() for ns in os.getenv("DEFAULT_NAMESPACES", "").split(",") if ns.strip()
]

API_PORT = int(os.getenv("API_PORT", "8080"))

# Browser origins allowed to call /api/*. Defaults to EMPTY, meaning no
# cross-origin access. The built-in web UI is served by this same app at "/", so
# its requests are same-origin and need no grant. A previous "*" meant any page
# the operator visited while `kubectl port-forward` was open could read
# /api/audit, which exposes the arguments of approved cluster mutations.
CORS_ALLOW_ORIGINS = [
    o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "").split(",") if o.strip()
]

# ---------------------------------------------------------------------------
# Cost / runaway-loop safety limits
# ---------------------------------------------------------------------------
# A langgraph "super-step" cap. This is the hard backstop that guarantees the
# graph cannot loop forever (e.g. the agent grep/read_file-ing files in a
# cycle). Applied to every agent.invoke via make_agent_config().
RECURSION_LIMIT = int(os.getenv("AGENT_RECURSION_LIMIT", "60"))

# Max model (LLM) calls. run_limit = per single invocation; thread_limit =
# cumulative across a conversation thread. Backstop against runaway token spend.
MODEL_CALL_RUN_LIMIT = int(os.getenv("MODEL_CALL_RUN_LIMIT", "40"))
MODEL_CALL_THREAD_LIMIT = int(os.getenv("MODEL_CALL_THREAD_LIMIT", "120"))

# Max total tool calls in a single run, and a tighter cap on the read-heavy
# filesystem tools that caused the original grep/read_file cost incident.
TOOL_CALL_RUN_LIMIT = int(os.getenv("TOOL_CALL_RUN_LIMIT", "80"))
FS_TOOL_RUN_LIMIT = int(os.getenv("FS_TOOL_RUN_LIMIT", "25"))

# Max characters any single tool result may contribute to the message history.
# Every other limit in this file counts CALLS; none bounded bytes, and that is how
# a run reached a 632,740-token prompt against a 200,000 ceiling while satisfying
# all of them: 80 tool calls x ~8k tokens = 640k. Summarization cannot rescue that,
# because it triggers at 170k and therefore has only 30k of headroom, while a
# single fan-out step was observed adding ~580k.
# Sized against that 30k headroom rather than a round number: at ~4 chars per
# token, 12000 chars is ~3k tokens, so an 8-way parallel fan-out adds ~24k and
# still fits. Only one output observed in production exceeded it (20,451 chars).
TOOL_OUTPUT_MAX_CHARS = int(os.getenv("TOOL_OUTPUT_MAX_CHARS", "12000"))

# Anthropic prompt caching — caches the large static system prompt + tool
# definitions + growing message history so a multi-step loop is not re-billed
# full input tokens on every model call. It is ignored for OpenAI, whose recent
# models use automatic prompt caching.
PROMPT_CACHING = os.getenv("PROMPT_CACHING", "true").lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# Durable state
# ---------------------------------------------------------------------------
# Postgres DSN backing the langgraph checkpointer/store, the session table, the
# HITL audit log, and monitoring finding-state. When unset the process falls
# back to in-memory equivalents (fine for `python main.py` locally, but pending
# HITL approvals and monitoring state do NOT survive a restart).
DATABASE_URL = os.getenv("DATABASE_URL", "")

# ---------------------------------------------------------------------------
# Monitoring
# ---------------------------------------------------------------------------
# Whether the background scheduler runs at all. This env var was previously
# documented and set in k8s/deployment.yaml but read by NO code, so the
# scheduler started unconditionally and MONITORING_ENABLED="false" was silently
# ignored. Defaults to true to preserve that de-facto behaviour when unset.
MONITORING_ENABLED = os.getenv("MONITORING_ENABLED", "true").lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# Monitoring notification policy
# ---------------------------------------------------------------------------
# The scheduled health check posts to Slack only when the diff against the
# previous run contains something new, escalated, or newly resolved. Without
# this a steady-state cluster problem is re-reported every interval forever,
# which is what made the scheduler too noisy to leave enabled.
MONITOR_NOTIFY_ON_RESOLVED = os.getenv("MONITOR_NOTIFY_ON_RESOLVED", "true").lower() in ("1", "true", "yes")

# Force a full report every N checks even when nothing changed, so a quiet
# channel still proves the bot is alive. 0 disables the digest entirely.
MONITOR_DIGEST_EVERY_N_CHECKS = int(os.getenv("MONITOR_DIGEST_EVERY_N_CHECKS", "12"))

# How long the Slack "Ack" button suppresses a finding from notifications.
MONITOR_ACK_HOURS = int(os.getenv("MONITOR_ACK_HOURS", "24"))

# ---------------------------------------------------------------------------
# HITL authorization
# ---------------------------------------------------------------------------
# Comma-separated Slack user IDs (e.g. "U123ABC,U456DEF") allowed to approve or
# reject cluster mutations. EMPTY MEANS ANY workspace user who can see the
# message may approve — which is the pre-existing behaviour, preserved as the
# default so enabling durability does not silently lock anyone out. Set this to
# lock approvals down to a named on-call group.
SLACK_APPROVER_IDS = {
    uid.strip() for uid in os.getenv("SLACK_APPROVER_IDS", "").split(",") if uid.strip()
}


def make_agent_config(thread_id: str) -> dict:
    """Build the langgraph invoke config for an agent run.

    Centralises the recursion_limit so every invoke / HITL resume across the
    API, Slack, scheduler, and CLI enforces the same hard loop cap.
    """
    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": RECURSION_LIMIT,
    }
