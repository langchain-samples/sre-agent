"""FastAPI server — SSE streaming, HITL endpoints, Slack Bolt, and monitoring scheduler."""
from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from langgraph.types import Command
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from config import (
    CORS_ALLOW_ORIGINS,
    DATABASE_URL,
    MONITOR_ACK_HOURS,
    MONITORING_ENABLED,
    SLACK_APPROVER_IDS,
    make_agent_config,
)
from response_text import response_text

load_dotenv()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sre-agent")

# A "health check / audit" request is served by the bounded, single-model-call
# structured path (scheduler.run_structured_health_check), NOT the full Deep
# Agents orchestrator — the orchestrator fans out to subagents and can blow past
# the langgraph recursion limit. Anything not matching this falls through to the
# agent as normal.
_HEALTH_CHECK_RE = re.compile(
    r"\b(health\s*[-]?\s*(check|audit|report|status)"
    r"|(cluster|cluster's)\s+health"
    r"|audit\s+(the\s+)?cluster"
    r"|(is|how'?s)\s+(the\s+)?cluster\s+(health|healthy|doing))\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

class SessionStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    DONE = "done"
    ERROR = "error"


@dataclass
class Session:
    id: str
    thread_id: str
    status: SessionStatus = SessionStatus.IDLE
    interrupt_data: Any = None
    pending_decisions: int = 1           # number of tool calls awaiting approval
    # Structured {"name": tool, "args": {...}} for each pending call. Kept
    # alongside interrupt_data (which is display-only stringified interrupts)
    # because the audit log has to record *what* was approved, not a repr.
    pending_actions: list = field(default_factory=list)
    last_response: str = ""
    source: str = "api"                  # 'api' | 'scheduler' | 'slack'
    slack_message_ts: Optional[str] = None
    slack_channel: Optional[str] = None  # channel to post responses back to
    slack_thread_ts: Optional[str] = None  # thread to reply in
    pending_hitl_actor: Optional[str] = None      # set on click, cleared on finalize
    pending_hitl_approved: Optional[bool] = None  # set on click, cleared on finalize
    event_queue: asyncio.Queue = field(default_factory=asyncio.Queue)

    def to_row(self) -> dict:
        """Durable fields only — event_queue is per-process and not persisted."""
        return {
            "id": self.id,
            "thread_id": self.thread_id,
            "status": self.status.value,
            "source": self.source,
            "pending_decisions": self.pending_decisions,
            "pending_actions": self.pending_actions,
            "interrupt_data": self.interrupt_data,
            "last_response": self.last_response,
            "slack_message_ts": self.slack_message_ts,
            "slack_channel": self.slack_channel,
            "slack_thread_ts": self.slack_thread_ts,
        }

    @classmethod
    def from_row(cls, row: dict) -> "Session":
        """Rebuild a session from Postgres with a fresh event queue."""
        return cls(
            id=row["id"],
            thread_id=row["thread_id"],
            status=SessionStatus(row["status"]),
            source=row.get("source") or "api",
            pending_decisions=row.get("pending_decisions") or 1,
            pending_actions=list(row.get("pending_actions") or []),
            interrupt_data=row.get("interrupt_data"),
            last_response=row.get("last_response") or "",
            slack_message_ts=row.get("slack_message_ts"),
            slack_channel=row.get("slack_channel"),
            slack_thread_ts=row.get("slack_thread_ts"),
        )


_sessions: dict[str, Session] = {}
_executor = ThreadPoolExecutor(max_workers=4)
_notifier = None   # set in lifespan
_agent = None      # set in lifespan
_scheduler = None  # set in lifespan
_db = None         # set in lifespan (persistence.PostgresDatabase | NullDatabase)


def _save(session: Session) -> None:
    """Write a session through to Postgres.

    Best-effort by design: losing durability is bad, but it must never take down
    an in-flight cluster diagnosis. Failures are logged and surfaced by /health.
    """
    if _db is None:
        return
    try:
        _db.save_session(session.to_row())
    except Exception:
        log.exception("Failed to persist session %s", session.id)


def _track(session: Session) -> Session:
    _sessions[session.id] = session
    _save(session)
    return session


def _recover_session(session_id: str) -> Optional[Session]:
    """Return a session from memory, falling back to Postgres.

    This is what makes a Slack Approve button survive a pod restart: the
    langgraph checkpoint still holds the interrupted graph under `thread_id`, so
    once the Session row is rehydrated the resume proceeds normally.
    """
    if session_id in _sessions:
        return _sessions[session_id]
    if _db is None:
        return None
    try:
        row = _db.load_session(session_id)
    except Exception:
        log.exception("Failed to load session %s", session_id)
        return None
    if not row:
        return None
    session = Session.from_row(row)
    _sessions[session_id] = session
    log.info(
        "Recovered session %s from Postgres (status=%s, pending=%d)",
        session.id, session.status, session.pending_decisions,
    )
    return session


def _get_session(session_id: str) -> Session:
    session = _recover_session(session_id)
    if session is None:
        raise HTTPException(404, f"Session '{session_id}' not found")
    return session


def _extract_action_requests(interrupts) -> list[dict]:
    """Pull structured {"name", "args"} pairs out of langgraph interrupts."""
    actions: list[dict] = []
    for interrupt in interrupts:
        val = interrupt.value if hasattr(interrupt, "value") else {}
        if not isinstance(val, dict):
            continue
        for req in val.get("action_requests", []) or []:
            if isinstance(req, dict):
                actions.append({
                    "name": req.get("name") or req.get("action") or "",
                    "args": req.get("args") or req.get("arguments") or {},
                })
    return actions


def _audit(session: Session, decision: str, actor: str = "", actor_id: str = "",
           result: str = "") -> None:
    """Append one immutable audit row per tool call covered by this decision."""
    if _db is None:
        return
    actions = session.pending_actions or []
    try:
        if not actions:
            _db.record_decision(
                session_id=session.id, thread_id=session.thread_id,
                decision=decision, actor=actor, actor_id=actor_id,
                source=session.source, result=result,
            )
            return
        for action in actions:
            _db.record_decision(
                session_id=session.id, thread_id=session.thread_id,
                decision=decision, actor=actor, actor_id=actor_id,
                source=session.source, tool_name=action.get("name", ""),
                tool_args=action.get("args") or {}, result=result,
            )
    except Exception:
        log.exception("Failed to write HITL audit row (session=%s)", session.id)


def _approver_allowed(user_id: str) -> bool:
    """Whether a Slack user may approve/reject a cluster mutation.

    An empty SLACK_APPROVER_IDS keeps the historical behaviour — anyone who can
    see the message can click — so turning on durability does not lock out an
    existing on-call rotation. Set it to restrict approvals to named users.
    """
    if not SLACK_APPROVER_IDS:
        return True
    return user_id in SLACK_APPROVER_IDS


# ---------------------------------------------------------------------------
# Agent runner helpers (run in thread pool to avoid blocking event loop)
# ---------------------------------------------------------------------------

def _finalize_hitl_message(session: Session, result_text: str = ""):
    """If a HITL click was registered on this session, swap the 'processing' Slack
    message for the final verdict and clear the pending state."""
    if not (_notifier and session.slack_message_ts and session.pending_hitl_actor is not None):
        return
    _notifier.update_hitl_resolved(
        session.slack_message_ts,
        approved=bool(session.pending_hitl_approved),
        actor=session.pending_hitl_actor,
        result=result_text,
    )
    session.pending_hitl_actor = None
    session.pending_hitl_approved = None
    session.slack_message_ts = None
    _save(session)


def _handle_result(result: dict, session: Session, loop):
    """Process an agent result — put events on the session queue and send Slack msgs."""
    # Resolve any in-flight HITL message before posting a new one
    _finalize_hitl_message(session)

    interrupts = result.get("__interrupt__", [])

    if interrupts:
        session.status = SessionStatus.INTERRUPTED
        session.interrupt_data = [str(i) for i in interrupts]
        session.pending_actions = _extract_action_requests(interrupts)
        # Resume must send exactly one decision per pending tool call.
        session.pending_decisions = max(len(session.pending_actions), 1)

        # Persist before notifying: the Slack button must never point at a
        # session that was not written down.
        _save(session)

        # Notify via Slack
        if _notifier and _notifier.enabled:
            ts = _notifier.send_hitl_request(
                session.id,
                "\n".join(session.interrupt_data),
            )
            session.slack_message_ts = ts
            _save(session)

        asyncio.run_coroutine_threadsafe(
            session.event_queue.put({"type": "interrupt", "data": session.interrupt_data}),
            loop,
        )
    else:
        messages = result.get("messages", [])
        response = ""
        if messages:
            response = response_text(messages[-1])
        session.last_response = response
        session.status = SessionStatus.DONE
        session.pending_actions = []
        session.interrupt_data = None
        _save(session)

        # For scheduler-originated sessions, send Slack health report
        if session.source == "scheduler" and _notifier and _notifier.enabled:
            has_issues = any(
                kw in response.lower()
                for kw in ("critical", "crashloop", "oomkilled", "not ready", "evicted")
            )
            _notifier.send_health_report(response, has_issues=has_issues, source="scheduled")

        asyncio.run_coroutine_threadsafe(
            session.event_queue.put({"type": "done", "data": response}),
            loop,
        )
        todos = result.get("todos", [])
        if todos:
            asyncio.run_coroutine_threadsafe(
                session.event_queue.put({"type": "todos", "data": todos}),
                loop,
            )


def _run_agent_sync(agent, messages: list[dict], config: dict, session: Session, loop):
    try:
        result = agent.invoke({"messages": messages}, config=config)
        _handle_result(result, session, loop)
    except Exception as e:
        log.exception("Agent error (session=%s)", session.id)
        session.status = SessionStatus.ERROR
        _save(session)
        asyncio.run_coroutine_threadsafe(
            session.event_queue.put({"type": "error", "data": str(e)}), loop
        )


def _resume_agent_sync(agent, command: Command, config: dict, session: Session, loop):
    try:
        result = agent.invoke(command, config=config)
        _handle_result(result, session, loop)
    except Exception as e:
        log.exception("Resume error (session=%s)", session.id)
        session.status = SessionStatus.ERROR
        _save(session)
        asyncio.run_coroutine_threadsafe(
            session.event_queue.put({"type": "error", "data": str(e)}), loop
        )


def _do_approve(session: Session, loop):
    """Approve a HITL interrupt — usable from both API handlers and Slack Bolt threads."""
    session.status = SessionStatus.RUNNING
    _save(session)
    session.event_queue = asyncio.Queue()
    config = make_agent_config(session.thread_id)
    loop.run_in_executor(
        _executor,
        _resume_agent_sync,
        _agent,
        Command(resume={"decisions": [{"type": "approve"}] * session.pending_decisions}),
        config,
        session,
        loop,
    )


def _do_reject(session: Session, reason: str, loop):
    """Reject a HITL interrupt — usable from both API handlers and Slack Bolt threads."""
    session.status = SessionStatus.RUNNING
    _save(session)
    session.event_queue = asyncio.Queue()
    config = make_agent_config(session.thread_id)
    loop.run_in_executor(
        _executor,
        _resume_agent_sync,
        _agent,
        Command(resume={"decisions": [{"type": "reject", "message": reason}] * session.pending_decisions}),
        config,
        session,
        loop,
    )


# ---------------------------------------------------------------------------
# Slack chat helpers
# ---------------------------------------------------------------------------

_SLACK_MAX = 3000  # Slack text block character limit


def _post_long_response(client, channel: str, thread_ts: str, thinking_ts: str, response: str):
    """Post a response to Slack, handling long content gracefully.

    - Under 3000 chars: update the thinking message in place.
    - Over 3000 chars: update thinking message with a summary line, then upload
      the full content as a file snippet so nothing gets cut off.
    """
    if len(response) <= _SLACK_MAX:
        client.chat_update(channel=channel, ts=thinking_ts, text=response)
        return

    # First line of the response as a short summary
    summary = response.splitlines()[0][:200]
    client.chat_update(
        channel=channel,
        ts=thinking_ts,
        text=f"{summary}\n\n_Full output uploaded as a file below._",
    )
    client.files_upload_v2(
        channel=channel,
        thread_ts=thread_ts,
        content=response,
        filename="sre-agent-response.txt",
        title="Full Response",
    )


def _post_agent_result_to_slack(result: dict, session: Session, client, channel: str,
                                  thread_ts: str, thinking_ts: str):
    """Process agent result and update the Slack thinking message with the response."""
    interrupts = result.get("__interrupt__", [])
    if interrupts:
        session.status = SessionStatus.INTERRUPTED
        session.interrupt_data = [str(i) for i in interrupts]
        session.slack_channel = channel
        session.slack_thread_ts = thread_ts
        session.pending_actions = _extract_action_requests(interrupts)
        session.pending_decisions = max(len(session.pending_actions), 1)
        _save(session)

        if _notifier and _notifier.enabled:
            ts = _notifier.send_hitl_request(session.id, "\n".join(session.interrupt_data))
            session.slack_message_ts = ts
            _save(session)

        alerts_channel = os.getenv("SLACK_CHANNEL", "#sre-alerts")
        client.chat_update(
            channel=channel,
            ts=thinking_ts,
            text=f":warning: Action requires your approval — check {alerts_channel}",
        )
    else:
        messages = result.get("messages", [])
        response = ""
        if messages:
            response = response_text(messages[-1])
        session.last_response = response
        session.status = SessionStatus.DONE
        session.pending_actions = []
        session.interrupt_data = None
        _save(session)
        _post_long_response(client, channel, thread_ts, thinking_ts, response or "(no response)")


def _run_for_slack(text: str, session: Session, client, channel: str, thread_ts: str):
    """Run the agent for a Slack message and post the result back to the thread."""
    if not _agent:
        return
    thinking = client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=":hourglass_flowing_sand: Working on it...",
    )
    try:
        config = make_agent_config(session.thread_id)
        result = _agent.invoke({"messages": [{"role": "user", "content": text}]}, config=config)
        _post_agent_result_to_slack(result, session, client, channel, thread_ts, thinking["ts"])
    except Exception as e:
        session.status = SessionStatus.ERROR
        _save(session)
        log.exception("Slack agent error (session=%s)", session.id)
        client.chat_update(channel=channel, ts=thinking["ts"], text=f":red_circle: Error: {e}")


def _run_structured_health_check_for_slack(text: str, session: Session, client, channel: str, thread_ts: str):
    """Serve a health-check mention via the bounded single-model path (no orchestrator).

    This performs a fixed number of steps (zero-token collection + one structured
    model call) so it can never hit the recursion limit that the full agent does.
    """
    thinking = client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=":mag: Running a cluster health check...",
    )
    try:
        from scheduler import annotate_with_history, run_structured_health_check

        report, data = run_structured_health_check()
        # Read-only diff: annotates each finding with age / occurrence count
        # without advancing the counters, which stay owned by the scheduler so
        # ad-hoc requests cannot inflate them. Skipped entirely without a
        # database, since with no history every finding would be labelled NEW
        # on every run, which is worse than showing no label at all.
        diff = (
            annotate_with_history(report, _db)
            if (_db is not None and _db.available) else None
        )
        log.info(
            "Slack health check complete (session=%s, severity=%s, findings=%d, %s)",
            session.id, report.overall_severity, len(report.findings),
            diff.summary_line() if diff else "no history",
        )
        if _notifier and _notifier.enabled:
            _notifier.send_structured_report(
                report, source="slack", channel=channel, thread_ts=thread_ts,
                diff=diff,
            )
            # The structured report is posted as its own threaded message; drop the
            # transient placeholder (best-effort — ignore missing chat:write scope).
            try:
                client.chat_delete(channel=channel, ts=thinking["ts"])
            except Exception:
                client.chat_update(channel=channel, ts=thinking["ts"],
                                   text=":white_check_mark: Health check complete.")
        else:
            client.chat_update(channel=channel, ts=thinking["ts"],
                               text=report.summary or "(no findings)")
        session.last_response = report.summary
        session.status = SessionStatus.DONE
        _save(session)
    except Exception as e:
        session.status = SessionStatus.ERROR
        _save(session)
        log.exception("Slack health check error (session=%s)", session.id)
        client.chat_update(channel=channel, ts=thinking["ts"], text=f":red_circle: Error: {e}")


def _resume_for_slack(command, session: Session, client):
    """Resume a HITL-interrupted session and post the result back to the original thread."""
    if not _agent:
        return
    channel = session.slack_channel
    thread_ts = session.slack_thread_ts
    if not channel or not thread_ts:
        return
    thinking = client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=":hourglass_flowing_sand: Continuing...",
    )
    try:
        config = make_agent_config(session.thread_id)
        result = _agent.invoke(command, config=config)
        _finalize_hitl_message(session)
        _post_agent_result_to_slack(result, session, client, channel, thread_ts, thinking["ts"])
    except Exception as e:
        session.status = SessionStatus.ERROR
        _save(session)
        log.exception("Slack resume error (session=%s)", session.id)
        _finalize_hitl_message(session, result_text=f"Error: {e}")
        client.chat_update(channel=channel, ts=thinking["ts"], text=f":red_circle: Error: {e}")


# ---------------------------------------------------------------------------
# Slack Bolt (Socket Mode — no public ingress required)
# ---------------------------------------------------------------------------

def _start_slack_bolt(main_loop: asyncio.AbstractEventLoop):
    """Start the Slack Bolt Socket Mode handler in a background thread."""
    bot_token = os.getenv("SLACK_BOT_TOKEN", "")
    app_token = os.getenv("SLACK_APP_TOKEN", "")
    if not bot_token or not app_token:
        log.info("SLACK_BOT_TOKEN or SLACK_APP_TOKEN not set — Slack Bolt not started")
        return

    try:
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler

        import re

        bolt = App(token=bot_token)

        @bolt.event("app_mention")
        def handle_mention(event, body, client):
            # Ignore Slack delivery retries — the first invocation is already in-flight
            if body.get("headers", {}).get("x-slack-retry-num"):
                return

            # Strip the @mention from the text
            text = re.sub(r"<@[A-Z0-9]+>", "", event.get("text", "")).strip()
            if not text:
                client.chat_postMessage(
                    channel=event["channel"],
                    thread_ts=event["ts"],
                    text="Hi! Ask me anything about your cluster — e.g. _grab logs from pod X_ or _run a health audit_.",
                )
                return

            channel = event["channel"]
            # Use the thread root as the thread_id so context is preserved per thread
            thread_ts = event.get("thread_ts") or event["ts"]
            session_id = f"slack-{thread_ts}"

            session = _recover_session(session_id)
            if session is None:
                session = _track(Session(
                    id=session_id,
                    thread_id=session_id,
                    source="slack",
                    slack_channel=channel,
                    slack_thread_ts=thread_ts,
                ))
            else:
                if session.status in (SessionStatus.RUNNING, SessionStatus.INTERRUPTED):
                    log.info("Ignoring duplicate Slack event for session %s (status=%s)", session_id, session.status)
                    return
                session.status = SessionStatus.RUNNING
                session.slack_channel = channel
                session.slack_thread_ts = thread_ts
                _save(session)

            log.info("Slack mention from %s: %s", event.get("user"), text[:80])
            # Submit to executor so this handler returns immediately (prevents Slack retries).
            # Health-check/audit requests use the bounded single-model path so they can
            # never hit the orchestrator's recursion limit; everything else goes to the agent.
            if _HEALTH_CHECK_RE.search(text):
                _executor.submit(_run_structured_health_check_for_slack, text, session, client, channel, thread_ts)
            else:
                _executor.submit(_run_for_slack, text, session, client, channel, thread_ts)

        def _resolve_click(body, client, verb: str):
            """Shared guard for approve/reject clicks.

            Returns ``(session, actor, actor_id)`` or None once it has already
            told the user why the click did nothing. Every rejection path
            replies — a silent return leaves someone believing they stopped a
            cluster change when they did not.
            """
            session_id = body["actions"][0]["value"]
            user = body.get("user", {}) or {}
            actor = user.get("name") or "unknown"
            actor_id = user.get("id") or ""
            channel_id = (body.get("channel") or {}).get("id")

            def deny(text: str):
                try:
                    client.chat_postEphemeral(channel=channel_id, user=actor_id, text=text)
                except Exception:
                    log.exception("Failed to post ephemeral reply to %s", actor_id)

            if not _approver_allowed(actor_id):
                log.warning(
                    "Unauthorized %s attempt by %s (%s) on session %s",
                    verb, actor, actor_id, session_id,
                )
                deny(
                    f":no_entry: You are not authorised to {verb} cluster changes. "
                    "Approvals are restricted to the users in SLACK_APPROVER_IDS."
                )
                # Record the attempt — a denied approval is exactly the kind of
                # event an audit trail exists for.
                denied = _recover_session(session_id)
                if denied is not None:
                    _audit(
                        denied, f"{verb}-denied", actor=actor, actor_id=actor_id,
                        result="actor not in SLACK_APPROVER_IDS",
                    )
                return None

            session = _recover_session(session_id)
            if session is None:
                deny(
                    f":warning: Session `{session_id}` not found. It predates the current "
                    "database, or its record was removed."
                )
                return None
            if session.status != SessionStatus.INTERRUPTED:
                deny(
                    f":warning: Session `{session_id}` is not waiting for approval "
                    f"(status={session.status.value})."
                )
                return None
            return session, actor, actor_id

        @bolt.action("sre_approve")
        def handle_approve(ack, body, client):
            ack()
            resolved = _resolve_click(body, client, "approve")
            if resolved is None:
                return
            session, actor, actor_id = resolved
            log.info("Slack approve from %s for session %s", actor, session.id)
            _audit(session, "approve", actor=actor, actor_id=actor_id)

            if _notifier and session.slack_message_ts:
                _notifier.mark_hitl_processing(session.slack_message_ts, actor, "Approval")
            session.pending_hitl_actor = actor
            session.pending_hitl_approved = True
            _save(session)

            if session.source == "slack":
                session.status = SessionStatus.RUNNING
                _save(session)
                _executor.submit(
                    _resume_for_slack,
                    Command(resume={"decisions": [{"type": "approve"}] * session.pending_decisions}),
                    session,
                    client,
                )
            else:
                _do_approve(session, main_loop)

        @bolt.action("sre_reject")
        def handle_reject(ack, body, client):
            ack()
            resolved = _resolve_click(body, client, "reject")
            if resolved is None:
                return
            session, actor, actor_id = resolved
            log.info("Slack reject from %s for session %s", actor, session.id)
            _audit(session, "reject", actor=actor, actor_id=actor_id)

            reason = ""
            if _notifier and session.slack_message_ts:
                _notifier.mark_hitl_processing(session.slack_message_ts, actor, "Rejection")
            session.pending_hitl_actor = actor
            session.pending_hitl_approved = False
            _save(session)

            if session.source == "slack":
                session.status = SessionStatus.RUNNING
                _save(session)
                _executor.submit(
                    _resume_for_slack,
                    Command(resume={"decisions": [{"type": "reject", "message": reason}] * session.pending_decisions}),
                    session,
                    client,
                )
            else:
                _do_reject(session, reason, main_loop)

        @bolt.action("sre_ack")
        def handle_ack(ack, body, client):
            """Mute every finding in a monitoring report for MONITOR_ACK_HOURS.

            Not gated by SLACK_APPROVER_IDS: acking changes no cluster state, it
            only silences notifications, and an on-call engineer should be able
            to quiet a known issue without needing mutation rights.
            """
            ack()
            report_id = body["actions"][0]["value"]
            user = body.get("user", {}) or {}
            actor = user.get("name") or "unknown"
            channel_id = (body.get("channel") or {}).get("id")

            acked = 0
            if _db is not None:
                try:
                    acked = _db.ack_report(report_id, MONITOR_ACK_HOURS)
                except Exception:
                    log.exception("Ack failed (report=%s)", report_id)

            log.info("Slack ack from %s for report %s (%d findings)", actor, report_id, acked)
            if acked:
                client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=(body.get("message") or {}).get("ts"),
                    text=(
                        f":mute: {actor} acked {acked} finding(s) for {MONITOR_ACK_HOURS}h — "
                        "they will stay tracked but will not re-alert until then."
                    ),
                )
            else:
                try:
                    client.chat_postEphemeral(
                        channel=channel_id,
                        user=user.get("id", ""),
                        text=(
                            ":warning: Nothing to ack — these findings were already "
                            "resolved, or this report predates the current database."
                        ),
                    )
                except Exception:
                    log.exception("Failed to post ephemeral ack reply")

        handler = SocketModeHandler(bolt, app_token)
        log.info("Starting Slack Bolt Socket Mode handler")
        handler.start()
    except Exception:
        log.exception("Slack Bolt failed to start")


# ---------------------------------------------------------------------------
# FastAPI app + lifespan
# ---------------------------------------------------------------------------

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _notifier, _agent, _scheduler, _db

    # 1. Durable state (Postgres, or in-memory fallback). Built first so the
    #    agent, session table, audit log, and scheduler all share one pool.
    from persistence import init_persistence
    checkpointer, store, _db = init_persistence(DATABASE_URL)

    # 2. Slack notifier
    from slack_notifier import make_notifier
    _notifier = make_notifier()

    # 3. Slack tool
    from tools.slack import make_slack_notification_tool
    slack_tool = make_slack_notification_tool(_notifier)

    # 4. Agent (with Slack tool injected)
    from agent import create_sre_agent
    _agent = create_sre_agent(
        extra_tools=[slack_tool], checkpointer=checkpointer, store=store
    )

    # 5. Slack Bolt Socket Mode in background thread
    main_loop = asyncio.get_event_loop()
    threading.Thread(target=_start_slack_bolt, args=(main_loop,), daemon=True).start()

    # 6. Monitoring scheduler. MONITORING_ENABLED is now actually honoured — it
    #    was previously documented and set in the manifest but read by no code,
    #    so scheduled checks ran regardless of the flag.
    from scheduler import MonitoringScheduler
    interval = int(os.getenv("MONITOR_INTERVAL_MINUTES", "30"))
    _scheduler = MonitoringScheduler(_agent, _notifier, interval_minutes=interval, db=_db)
    if MONITORING_ENABLED:
        await _scheduler.start()
    else:
        log.info("MONITORING_ENABLED is false — scheduled health checks not started")

    if _notifier.enabled:
        _notifier.send_alert("ok", "SRE Bot Started", "The autonomous SRE bot is online and monitoring the cluster.")

    if not _db.available:
        log.warning(
            "Running with NO durable state — pending HITL approvals will not survive a "
            "restart and no audit trail is being written. Set DATABASE_URL to fix."
        )

    log.info(
        "SRE Bot ready (scheduler=%dm, slack=%s, state=%s, approvers=%s)",
        interval, _notifier.enabled, _db.kind,
        f"{len(SLACK_APPROVER_IDS)} allowlisted" if SLACK_APPROVER_IDS else "unrestricted",
    )
    yield

    # Shutdown
    if _scheduler:
        await _scheduler.stop()
    if _db is not None:
        _db.close()
    log.info("SRE Bot shutdown")


app = FastAPI(title="SRE Bot", version="1.0.0", lifespan=lifespan)
# Empty by default. The bundled UI is same-origin so it needs no CORS grant, and
# a wildcard here let any page read /api/audit through an open port-forward.
# NOTE: this blocks the browser path only. Anything that can reach the port
# directly still has unauthenticated access to every /api route.
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None


class ApproveRequest(BaseModel):
    session_id: str


class RejectRequest(BaseModel):
    session_id: str
    reason: str = ""


class EditRequest(BaseModel):
    session_id: str
    args: dict[str, Any]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    # Deliberately still "ok" when Postgres is down: this is the liveness probe,
    # and crash-looping the bot because its database is unreachable would remove
    # the operator's ability to ask it what is wrong. `durable_state` is the
    # field to alert on — it makes the degradation visible instead of silent.
    return {
        "status": "ok",
        "in_cluster": os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token"),
        "slack_enabled": _notifier.enabled if _notifier else False,
        "scheduler_running": _scheduler._running if _scheduler else False,
        "state_backend": _db.kind if _db else "uninitialized",
        "durable_state": _db.available if _db else False,
        "approvals_restricted": bool(SLACK_APPROVER_IDS),
    }


@app.get("/api/audit")
def audit_trail(limit: int = 50):
    """Recent HITL decisions — who approved or rejected which cluster change."""
    if _db is None or not _db.available:
        raise HTTPException(503, "No audit database configured (set DATABASE_URL)")
    rows = _db.recent_decisions(limit)
    return {
        "count": len(rows),
        "decisions": [
            {
                "ts": r["ts"].isoformat() if r.get("ts") else None,
                "session_id": r.get("session_id"),
                "actor": r.get("actor"),
                "actor_id": r.get("actor_id"),
                "decision": r.get("decision"),
                "source": r.get("source"),
                "tool_name": r.get("tool_name"),
                "tool_args": r.get("tool_args"),
                "result": r.get("result"),
            }
            for r in rows
        ],
    }


@app.post("/api/chat")
async def chat(req: ChatRequest):
    """Start or continue a conversation. Returns session_id for SSE streaming."""
    session = _recover_session(req.session_id) if req.session_id else None
    if session is None:
        session_id = str(uuid.uuid4())
        session = _track(Session(id=session_id, thread_id=session_id, source="api"))

    if session.status == SessionStatus.RUNNING:
        raise HTTPException(409, "Session is already running")

    session.status = SessionStatus.RUNNING
    _save(session)
    session.event_queue = asyncio.Queue()
    loop = asyncio.get_event_loop()
    loop.run_in_executor(
        _executor,
        _run_agent_sync,
        _agent,
        [{"role": "user", "content": req.message}],
        make_agent_config(session.thread_id),
        session,
        loop,
    )
    return {"session_id": session.id, "status": "running"}


@app.get("/api/sessions/{session_id}/events")
async def stream_events(session_id: str):
    """SSE stream — yields done / interrupt / todos / error / heartbeat events."""
    session = _get_session(session_id)

    async def generator():
        while True:
            try:
                event = await asyncio.wait_for(session.event_queue.get(), timeout=60.0)
                yield json.dumps(event)
                if event["type"] in ("done", "error"):
                    break
                if event["type"] == "interrupt":
                    break
            except asyncio.TimeoutError:
                yield json.dumps({"type": "heartbeat"})

    return EventSourceResponse(generator())


@app.get("/api/sessions/{session_id}")
def get_session_status(session_id: str):
    s = _get_session(session_id)
    return {
        "session_id": s.id,
        "status": s.status,
        "source": s.source,
        "last_response": s.last_response,
        "interrupt_data": s.interrupt_data,
    }


@app.post("/api/approve")
async def approve(req: ApproveRequest):
    """Approve a pending HITL action."""
    session = _get_session(req.session_id)
    if session.status != SessionStatus.INTERRUPTED:
        raise HTTPException(409, f"Session not interrupted (status={session.status})")
    if _notifier and session.slack_message_ts:
        _notifier.mark_hitl_processing(session.slack_message_ts, "api-user", "Approval")
    # NOTE: this endpoint is unauthenticated, so the audit trail can only record
    # "api-user". Slack clicks carry a real identity; HTTP approvals do not.
    _audit(session, "approve", actor="api-user")
    session.pending_hitl_actor = "api-user"
    session.pending_hitl_approved = True
    _do_approve(session, asyncio.get_event_loop())
    return {"session_id": session.id, "status": "running"}


@app.post("/api/reject")
async def reject(req: RejectRequest):
    """Reject a pending HITL action."""
    session = _get_session(req.session_id)
    if session.status != SessionStatus.INTERRUPTED:
        raise HTTPException(409, f"Session not interrupted (status={session.status})")
    if _notifier and session.slack_message_ts:
        _notifier.mark_hitl_processing(session.slack_message_ts, "api-user", "Rejection")
    _audit(session, "reject", actor="api-user", result=req.reason)
    session.pending_hitl_actor = "api-user"
    session.pending_hitl_approved = False
    _do_reject(session, req.reason, asyncio.get_event_loop())
    return {"session_id": session.id, "status": "running"}


@app.post("/api/edit")
async def edit(req: EditRequest):
    """Edit the proposed action arguments then resume."""
    session = _get_session(req.session_id)
    if session.status != SessionStatus.INTERRUPTED:
        raise HTTPException(409, f"Session not interrupted (status={session.status})")
    # An edit is an approval with modified arguments — record what was actually
    # authorised, not just that something was.
    _audit(session, "edit", actor="api-user", result=json.dumps(req.args)[:2000])
    session.status = SessionStatus.RUNNING
    _save(session)
    session.event_queue = asyncio.Queue()
    loop = asyncio.get_event_loop()
    loop.run_in_executor(
        _executor,
        _resume_agent_sync,
        _agent,
        Command(resume={"decisions": [{"type": "edit", "args": req.args}] * session.pending_decisions}),
        make_agent_config(session.thread_id),
        session,
        loop,
    )
    return {"session_id": session.id, "status": "running"}


@app.post("/api/trigger-check")
async def trigger_check():
    """Manually trigger an immediate health check outside the schedule."""
    if not _scheduler:
        raise HTTPException(503, "Scheduler not initialized")
    session_id = await _scheduler.trigger_now()
    return {"session_id": session_id, "status": "running"}


# ---------------------------------------------------------------------------
# Built-in web UI
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def ui():
    return HTMLResponse(content=_UI_HTML)


_UI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SRE Bot</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', sans-serif; background: #0f1117; color: #e2e8f0; height: 100vh; display: flex; flex-direction: column; }
  header { background: #1a1d2e; padding: 16px 24px; border-bottom: 1px solid #2d3748; display: flex; align-items: center; gap: 12px; }
  header h1 { font-size: 18px; font-weight: 600; color: #63b3ed; }
  .badge { background: #2d3748; color: #68d391; font-size: 11px; padding: 2px 8px; border-radius: 12px; }
  #chat { flex: 1; overflow-y: auto; padding: 24px; display: flex; flex-direction: column; gap: 16px; }
  .msg { max-width: 80%; padding: 12px 16px; border-radius: 12px; line-height: 1.6; white-space: pre-wrap; font-size: 14px; }
  .msg.user { align-self: flex-end; background: #2b6cb0; color: #fff; }
  .msg.bot { align-self: flex-start; background: #1a1d2e; border: 1px solid #2d3748; }
  .msg.bot p { margin: 0 0 10px; }
  .msg.bot p:last-child { margin-bottom: 0; }
  .msg.bot h1, .msg.bot h2, .msg.bot h3 { color: #f7fafc; line-height: 1.3; margin: 14px 0 8px; }
  .msg.bot h1 { font-size: 18px; } .msg.bot h2 { font-size: 16px; } .msg.bot h3 { font-size: 14px; }
  .msg.bot h1:first-child, .msg.bot h2:first-child, .msg.bot h3:first-child { margin-top: 0; }
  .msg.bot ul, .msg.bot ol { margin: 6px 0 10px 20px; }
  .msg.bot li { margin: 4px 0; }
  .msg.bot code { background: #2d3748; border-radius: 4px; padding: 1px 4px; color: #bee3f8; }
  .msg.bot pre { overflow-x: auto; background: #111827; border-radius: 6px; padding: 10px; margin: 8px 0; white-space: pre-wrap; }
  .msg.bot pre code { background: none; padding: 0; }
  .msg.bot a { color: #90cdf4; }
  .msg.bot .table-wrap { overflow-x: auto; margin: 10px 0; border: 1px solid #2d3748; border-radius: 6px; }
  .msg.bot table { width: 100%; min-width: max-content; border-collapse: collapse; white-space: normal; }
  .msg.bot th, .msg.bot td { padding: 8px 10px; text-align: left; vertical-align: top; border-bottom: 1px solid #2d3748; }
  .msg.bot th { color: #f7fafc; background: #111827; font-weight: 600; }
  .msg.bot tr:last-child td { border-bottom: 0; }
  .msg.typing { display: flex; align-items: center; gap: 8px; color: #a0aec0; font-style: italic; }
  .typing-dots { display: inline-flex; gap: 3px; }
  .typing-dots i { width: 5px; height: 5px; border-radius: 50%; background: #63b3ed; animation: pulse 1.2s infinite ease-in-out; }
  .typing-dots i:nth-child(2) { animation-delay: 0.15s; }
  .typing-dots i:nth-child(3) { animation-delay: 0.3s; }
  @keyframes pulse { 0%, 60%, 100% { opacity: .3; transform: scale(.8); } 30% { opacity: 1; transform: scale(1); } }
  .msg.interrupt { align-self: flex-start; background: #744210; border: 1px solid #d69e2e; color: #fefcbf; width: 100%; max-width: 100%; }
  .msg.error { background: #742a2a; border: 1px solid #fc8181; }
  .interrupt-actions { display: flex; gap: 8px; margin-top: 12px; }
  .interrupt-actions button { padding: 6px 16px; border-radius: 6px; border: none; cursor: pointer; font-size: 13px; font-weight: 600; }
  .btn-approve { background: #276749; color: #fff; }
  .btn-reject  { background: #822727; color: #fff; }
  #input-row { padding: 16px 24px; background: #1a1d2e; border-top: 1px solid #2d3748; display: flex; gap: 10px; }
  #msg-input { flex: 1; background: #2d3748; border: 1px solid #4a5568; color: #e2e8f0; border-radius: 8px; padding: 10px 14px; font-size: 14px; resize: none; }
  #msg-input:focus { outline: none; border-color: #63b3ed; }
  #send-btn { background: #2b6cb0; color: #fff; border: none; border-radius: 8px; padding: 10px 20px; cursor: pointer; font-weight: 600; }
  #send-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  #trigger-check:disabled { opacity: 0.45; cursor: not-allowed; }
  .shortcuts { display: flex; gap: 8px; padding: 8px 24px; flex-wrap: wrap; }
  .shortcut { background: #1a1d2e; border: 1px solid #2d3748; color: #a0aec0; border-radius: 6px; padding: 4px 12px; font-size: 12px; cursor: pointer; }
  .shortcut:hover { border-color: #63b3ed; color: #63b3ed; }
  .todos { font-size: 12px; color: #a0aec0; padding: 4px 0; }
  .todos span { margin-right: 10px; }
</style>
</head>
<body>
<header>
  <h1>☸ SRE Bot</h1>
  <span class="badge">Kubernetes</span>
  <span class="badge" id="slack-badge">Slack: checking...</span>
  <span class="badge" id="status-badge">Ready</span>
  <button id="trigger-check" onclick="triggerCheck()" style="margin-left:auto;background:#2d3748;color:#a0aec0;border:1px solid #4a5568;border-radius:6px;padding:4px 12px;font-size:12px;cursor:pointer">▶ Trigger Check Now</button>
</header>
<div id="chat"></div>
<div class="shortcuts">
  <span class="shortcut" onclick="quickSend('Run a full cluster health audit across all namespaces')">🔍 Full Audit</span>
  <span class="shortcut" onclick="quickSend('Check all pods for issues across all namespaces')">🔴 Pods</span>
  <span class="shortcut" onclick="quickSend('Analyze scaling and HPA configuration')">📈 Scaling</span>
  <span class="shortcut" onclick="quickSend('Analyze CPU and memory performance')">⚡ Performance</span>
  <span class="shortcut" onclick="quickSend('Scan logs for errors and anomalies')">📋 Logs</span>
</div>
<div id="input-row">
  <textarea id="msg-input" rows="2" placeholder="Ask the SRE bot..." onkeydown="onKey(event)"></textarea>
  <button id="send-btn" onclick="sendMessage()">Send</button>
</div>
<script>
let sessionId = null;
let eventSource = null;
let progressMessage = null;
let progressStartedAt = null;
let progressLabel = '';
let progressTimer = null;
let slackEnabled = false;

fetch('/health').then(r=>r.json()).then(d=>{
  slackEnabled = Boolean(d.slack_enabled);
  document.getElementById('slack-badge').textContent = 'Slack: ' + (slackEnabled ? 'on' : 'off');
  document.getElementById('slack-badge').style.color = slackEnabled ? '#68d391' : '#fc8181';
  const trigger = document.getElementById('trigger-check');
  trigger.disabled = !slackEnabled;
  trigger.title = slackEnabled
    ? 'Run a health check and deliver the summary to Slack.'
    : 'Slack integration is required.';
}).catch(() => {
  document.getElementById('slack-badge').textContent = 'Slack: unavailable';
});

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
}
function renderInline(text) {
  return text
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/[*][*]([^*]+)[*][*]/g, '<strong>$1</strong>')
    .replace(/__([^_]+)__/g, '<strong>$1</strong>')
    .replace(/[*]([^*]+)[*]/g, '<strong>$1</strong>')
    .replace(/_([^_]+)_/g, '<em>$1</em>')
    .replace(/\\[([^\\]]+)\\]\\((https?:\\/\\/[^\\s)]+)\\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
}
function renderMarkdown(text) {
  const lines = escapeHtml(text).split('\\n');
  const html = [];
  let list = null;
  let inCode = false;
  const closeList = () => { if (list) { html.push(`</${list}>`); list = null; } };
  const tableCells = (line) => line.trim().replace(/^\\|/, '').replace(/\\|$/, '').split('|').map(cell => cell.trim());
  const isTableSeparator = (line) => /^\\s*\\|?\\s*:?-{3,}:?\\s*(\\|\\s*:?-{3,}:?\\s*)+\\|?\\s*$/.test(line);
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (line.startsWith('```')) {
      closeList();
      html.push(inCode ? '</code></pre>' : '<pre><code>');
      inCode = !inCode;
      continue;
    }
    if (inCode) { html.push(line + '\\n'); continue; }
    const heading = line.match(/^(#{1,3})\\s+(.+)$/);
    const bullet = line.match(/^\\s*[-*+•]\\s+(.+)$/);
    const ordered = line.match(/^\\s*\\d+\\.\\s+(.+)$/);
    if (line.includes('|') && isTableSeparator(lines[index + 1] || '')) {
      closeList();
      const headers = tableCells(line);
      html.push('<div class="table-wrap"><table><thead><tr>');
      headers.forEach(cell => html.push(`<th>${renderInline(cell)}</th>`));
      html.push('</tr></thead><tbody>');
      index += 1;
      while (index + 1 < lines.length && lines[index + 1].includes('|') && lines[index + 1].trim()) {
        const cells = tableCells(lines[index + 1]);
        if (cells.length !== headers.length) break;
        html.push('<tr>');
        cells.forEach(cell => html.push(`<td>${renderInline(cell)}</td>`));
        html.push('</tr>');
        index += 1;
      }
      html.push('</tbody></table></div>');
    }
    else if (heading) { closeList(); const level = heading[1].length; html.push(`<h${level}>${renderInline(heading[2])}</h${level}>`); }
    else if (bullet) { if (list !== 'ul') { closeList(); html.push('<ul>'); list = 'ul'; } html.push(`<li>${renderInline(bullet[1])}</li>`); }
    else if (ordered) { if (list !== 'ol') { closeList(); html.push('<ol>'); list = 'ol'; } html.push(`<li>${renderInline(ordered[1])}</li>`); }
    else if (!line.trim()) { closeList(); }
    else { closeList(); html.push(`<p>${renderInline(line)}</p>`); }
  }
  closeList();
  if (inCode) html.push('</code></pre>');
  return html.join('');
}
function appendMsg(text, cls, markdown = false) {
  const chat = document.getElementById('chat');
  const div = document.createElement('div');
  div.className = 'msg ' + cls;
  if (markdown) div.innerHTML = renderMarkdown(text);
  else div.textContent = text;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return div;
}
function setStatus(s) { document.getElementById('status-badge').textContent = s; }
function setInputEnabled(v) {
  document.getElementById('send-btn').disabled = !v;
  document.getElementById('msg-input').disabled = !v;
}
function beginProgress(label) {
  endProgress();
  progressStartedAt = Date.now();
  progressLabel = label;
  progressMessage = appendMsg('', 'bot typing');
  progressMessage.innerHTML = `<span class="typing-dots"><i></i><i></i><i></i></span><span>${escapeHtml(label)}</span>`;
  updateProgress();
  progressTimer = window.setInterval(updateProgress, 1000);
}
function updateProgress(label) {
  if (!progressMessage) return;
  if (label) progressLabel = label;
  const elapsed = Math.max(1, Math.round((Date.now() - progressStartedAt) / 1000));
  progressMessage.lastElementChild.textContent = `${progressLabel} (${elapsed}s)`;
}
function endProgress() {
  if (progressTimer) window.clearInterval(progressTimer);
  if (progressMessage) progressMessage.remove();
  progressMessage = null;
  progressStartedAt = null;
  progressLabel = '';
  progressTimer = null;
}
async function sendMessage() {
  const input = document.getElementById('msg-input');
  const text = input.value.trim(); if (!text) return;
  input.value = '';
  appendMsg(text, 'user');
  setInputEnabled(false); setStatus('Running...');
  beginProgress('Analyzing your request…');
  try {
    const res = await fetch('/api/chat', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: text, session_id: sessionId}),
    });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    sessionId = data.session_id;
    updateProgress('Inspecting the cluster…');
    listenForEvents();
  } catch (error) {
    endProgress(); appendMsg('Error: ' + error.message, 'error'); setStatus('Error'); setInputEnabled(true);
  }
}
function listenForEvents() {
  if (eventSource) eventSource.close();
  eventSource = new EventSource('/api/sessions/' + sessionId + '/events');
  eventSource.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.type === 'done') {
      endProgress(); appendMsg(ev.data, 'bot', true); setStatus('Ready'); setInputEnabled(true); eventSource.close();
    } else if (ev.type === 'interrupt') {
      endProgress(); showInterrupt(ev.data); setStatus('Approval Required'); eventSource.close();
    } else if (ev.type === 'error') {
      endProgress(); appendMsg('Error: ' + ev.data, 'error'); setStatus('Error'); setInputEnabled(true); eventSource.close();
    } else if (ev.type === 'heartbeat') {
      updateProgress('Still working…');
    } else if (ev.type === 'todos') {
      showTodos(ev.data);
    }
  };
  eventSource.onerror = () => updateProgress('Waiting for the agent…');
}
function showInterrupt(data) {
  const chat = document.getElementById('chat');
  const div = document.createElement('div');
  div.className = 'msg interrupt';
  div.innerHTML = '<strong>⚠ Approval Required</strong> <em style="font-size:11px">(also sent to Slack)</em><br><pre style="margin-top:8px;font-size:12px;white-space:pre-wrap">' + data.join('\\n') + '</pre>';
  const actions = document.createElement('div');
  actions.className = 'interrupt-actions';
  const ab = document.createElement('button'); ab.className='btn-approve'; ab.textContent='✓ Approve';
  ab.onclick = () => respond('approve', div);
  const rb = document.createElement('button'); rb.className='btn-reject'; rb.textContent='✗ Reject';
  rb.onclick = () => { const reason = prompt('Reason (optional):') || ''; respond('reject', div, reason); };
  actions.appendChild(ab); actions.appendChild(rb); div.appendChild(actions);
  chat.appendChild(div); chat.scrollTop = chat.scrollHeight;
}
async function respond(decision, div, reason) {
  div.remove(); setStatus('Running...');
  beginProgress('Continuing with your decision…');
  const body = decision === 'reject' ? {session_id: sessionId, reason: reason||''} : {session_id: sessionId};
  try {
    const res = await fetch('/api/' + decision, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    if (!res.ok) throw new Error(await res.text());
    listenForEvents();
  } catch (error) {
    endProgress(); appendMsg('Error: ' + error.message, 'error'); setStatus('Error'); setInputEnabled(true);
  }
}
function showTodos(todos) {
  const icons = {completed:'✅', in_progress:'🔄', pending:'⏳'};
  const existing = document.getElementById('todos-bar');
  if (existing) existing.remove();
  const chat = document.getElementById('chat');
  const div = document.createElement('div');
  div.id = 'todos-bar'; div.className = 'todos';
  div.innerHTML = todos.map(t => `<span>${icons[t.status]||'•'} ${t.content}</span>`).join('');
  chat.appendChild(div); chat.scrollTop = chat.scrollHeight;
}
async function triggerCheck() {
  if (!slackEnabled) return;
  const trigger = document.getElementById('trigger-check');
  trigger.disabled = true;
  try {
    const res = await fetch('/api/trigger-check', {method:'POST'});
    if (!res.ok) throw new Error(await res.text());
    appendMsg('Slack health check started. Its summary will be delivered to Slack.', 'bot');
  } catch (error) {
    appendMsg('Error: ' + error.message, 'error');
  } finally {
    trigger.disabled = false;
  }
}
function quickSend(text) { document.getElementById('msg-input').value = text; sendMessage(); }
function onKey(e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); } }
</script>
</body>
</html>
"""

if __name__ == "__main__":
    import uvicorn
    from config import API_PORT
    uvicorn.run("api:app", host="0.0.0.0", port=API_PORT, reload=False)
