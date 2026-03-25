"""FastAPI server — SSE streaming, HITL endpoints, Slack Bolt, and monitoring scheduler."""
from __future__ import annotations
import asyncio
import json
import logging
import os
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

load_dotenv()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sre-agent")

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
    last_response: str = ""
    source: str = "api"                  # 'api' | 'scheduler' | 'slack'
    slack_message_ts: Optional[str] = None
    slack_channel: Optional[str] = None  # channel to post responses back to
    slack_thread_ts: Optional[str] = None  # thread to reply in
    event_queue: asyncio.Queue = field(default_factory=asyncio.Queue)


_sessions: dict[str, Session] = {}
_executor = ThreadPoolExecutor(max_workers=4)
_notifier = None   # set in lifespan
_agent = None      # set in lifespan
_scheduler = None  # set in lifespan


def _get_session(session_id: str) -> Session:
    if session_id not in _sessions:
        raise HTTPException(404, f"Session '{session_id}' not found")
    return _sessions[session_id]


# ---------------------------------------------------------------------------
# Agent runner helpers (run in thread pool to avoid blocking event loop)
# ---------------------------------------------------------------------------

def _handle_result(result: dict, session: Session, loop):
    """Process an agent result — put events on the session queue and send Slack msgs."""
    interrupts = result.get("__interrupt__", [])

    if interrupts:
        session.status = SessionStatus.INTERRUPTED
        session.interrupt_data = [str(i) for i in interrupts]

        # Notify via Slack
        if _notifier and _notifier.enabled:
            ts = _notifier.send_hitl_request(
                session.id,
                "\n".join(session.interrupt_data),
            )
            session.slack_message_ts = ts

        asyncio.run_coroutine_threadsafe(
            session.event_queue.put({"type": "interrupt", "data": session.interrupt_data}),
            loop,
        )
    else:
        messages = result.get("messages", [])
        response = ""
        if messages:
            last = messages[-1]
            response = last.content if hasattr(last, "content") else str(last)
        session.last_response = response
        session.status = SessionStatus.DONE

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
        asyncio.run_coroutine_threadsafe(
            session.event_queue.put({"type": "error", "data": str(e)}), loop
        )


def _do_approve(session: Session, loop):
    """Approve a HITL interrupt — usable from both API handlers and Slack Bolt threads."""
    session.status = SessionStatus.RUNNING
    session.event_queue = asyncio.Queue()
    config = {"configurable": {"thread_id": session.thread_id}}
    loop.run_in_executor(
        _executor,
        _resume_agent_sync,
        _agent,
        Command(resume={"decisions": [{"type": "approve"}]}),
        config,
        session,
        loop,
    )


def _do_reject(session: Session, reason: str, loop):
    """Reject a HITL interrupt — usable from both API handlers and Slack Bolt threads."""
    session.status = SessionStatus.RUNNING
    session.event_queue = asyncio.Queue()
    config = {"configurable": {"thread_id": session.thread_id}}
    loop.run_in_executor(
        _executor,
        _resume_agent_sync,
        _agent,
        Command(resume={"decisions": [{"type": "reject", "message": reason}]}),
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

        if _notifier and _notifier.enabled:
            ts = _notifier.send_hitl_request(session.id, "\n".join(session.interrupt_data))
            session.slack_message_ts = ts

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
            last = messages[-1]
            response = last.content if hasattr(last, "content") else str(last)
        session.last_response = response
        session.status = SessionStatus.DONE
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
        config = {"configurable": {"thread_id": session.thread_id}}
        result = _agent.invoke({"messages": [{"role": "user", "content": text}]}, config=config)
        _post_agent_result_to_slack(result, session, client, channel, thread_ts, thinking["ts"])
    except Exception as e:
        session.status = SessionStatus.ERROR
        log.exception("Slack agent error (session=%s)", session.id)
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
        config = {"configurable": {"thread_id": session.thread_id}}
        result = _agent.invoke(command, config=config)
        _post_agent_result_to_slack(result, session, client, channel, thread_ts, thinking["ts"])
    except Exception as e:
        session.status = SessionStatus.ERROR
        log.exception("Slack resume error (session=%s)", session.id)
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
        def handle_mention(event, client):
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

            if session_id not in _sessions:
                session = Session(
                    id=session_id,
                    thread_id=session_id,
                    source="slack",
                    slack_channel=channel,
                    slack_thread_ts=thread_ts,
                )
                _sessions[session_id] = session
            else:
                session = _sessions[session_id]
                session.status = SessionStatus.RUNNING
                session.slack_channel = channel
                session.slack_thread_ts = thread_ts

            log.info("Slack mention from %s: %s", event.get("user"), text[:80])
            _run_for_slack(text, session, client, channel, thread_ts)

        @bolt.action("sre_approve")
        def handle_approve(ack, body, client):
            ack()
            session_id = body["actions"][0]["value"]
            actor = body.get("user", {}).get("name", "unknown")
            log.info("Slack approve from %s for session %s", actor, session_id)

            if session_id not in _sessions:
                client.chat_postEphemeral(
                    channel=body["channel"]["id"],
                    user=body["user"]["id"],
                    text=f":warning: Session `{session_id}` not found — it may have expired.",
                )
                return

            session = _sessions[session_id]
            if session.status != SessionStatus.INTERRUPTED:
                client.chat_postEphemeral(
                    channel=body["channel"]["id"],
                    user=body["user"]["id"],
                    text=f":warning: Session `{session_id}` is not waiting for approval (status={session.status}).",
                )
                return

            if _notifier and session.slack_message_ts:
                _notifier.update_hitl_resolved(
                    session.slack_message_ts, approved=True, actor=actor
                )

            if session.source == "slack":
                session.status = SessionStatus.RUNNING
                _executor.submit(
                    _resume_for_slack,
                    Command(resume={"decisions": [{"type": "approve"}]}),
                    session,
                    client,
                )
            else:
                _do_approve(session, main_loop)

        @bolt.action("sre_reject")
        def handle_reject(ack, body, client):
            ack()
            session_id = body["actions"][0]["value"]
            actor = body.get("user", {}).get("name", "unknown")
            log.info("Slack reject from %s for session %s", actor, session_id)

            if session_id not in _sessions:
                return

            session = _sessions[session_id]
            if session.status != SessionStatus.INTERRUPTED:
                return

            reason = ""
            if _notifier and session.slack_message_ts:
                _notifier.update_hitl_resolved(
                    session.slack_message_ts, approved=False, actor=actor
                )

            if session.source == "slack":
                session.status = SessionStatus.RUNNING
                _executor.submit(
                    _resume_for_slack,
                    Command(resume={"decisions": [{"type": "reject", "message": reason}]}),
                    session,
                    client,
                )
            else:
                _do_reject(session, reason, main_loop)

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
    global _notifier, _agent, _scheduler

    # 1. Slack notifier
    from slack_notifier import make_notifier
    _notifier = make_notifier()

    # 2. Slack tool
    from tools.slack import make_slack_notification_tool
    slack_tool = make_slack_notification_tool(_notifier)

    # 3. Agent (with Slack tool injected)
    from agent import create_sre_agent
    _agent = create_sre_agent(extra_tools=[slack_tool])

    # 4. Slack Bolt Socket Mode in background thread
    main_loop = asyncio.get_event_loop()
    threading.Thread(target=_start_slack_bolt, args=(main_loop,), daemon=True).start()

    # 5. Monitoring scheduler
    from scheduler import MonitoringScheduler
    interval = int(os.getenv("MONITOR_INTERVAL_MINUTES", "30"))
    _scheduler = MonitoringScheduler(_agent, _notifier, interval_minutes=interval)
    await _scheduler.start()

    if _notifier.enabled:
        _notifier.send_alert("ok", "SRE Bot Started", "The autonomous SRE bot is online and monitoring the cluster.")

    log.info("SRE Bot ready (scheduler=%dm, slack=%s)", interval, _notifier.enabled)
    yield

    # Shutdown
    if _scheduler:
        await _scheduler.stop()
    log.info("SRE Bot shutdown")


app = FastAPI(title="SRE Bot", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
    return {
        "status": "ok",
        "in_cluster": os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token"),
        "slack_enabled": _notifier.enabled if _notifier else False,
        "scheduler_running": _scheduler._running if _scheduler else False,
    }


@app.post("/api/chat")
async def chat(req: ChatRequest):
    """Start or continue a conversation. Returns session_id for SSE streaming."""
    if req.session_id and req.session_id in _sessions:
        session = _sessions[req.session_id]
    else:
        session_id = str(uuid.uuid4())
        session = Session(id=session_id, thread_id=session_id, source="api")
        _sessions[session_id] = session

    if session.status == SessionStatus.RUNNING:
        raise HTTPException(409, "Session is already running")

    session.status = SessionStatus.RUNNING
    session.event_queue = asyncio.Queue()
    loop = asyncio.get_event_loop()
    loop.run_in_executor(
        _executor,
        _run_agent_sync,
        _agent,
        [{"role": "user", "content": req.message}],
        {"configurable": {"thread_id": session.thread_id}},
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
    _do_approve(session, asyncio.get_event_loop())
    return {"session_id": session.id, "status": "running"}


@app.post("/api/reject")
async def reject(req: RejectRequest):
    """Reject a pending HITL action."""
    session = _get_session(req.session_id)
    if session.status != SessionStatus.INTERRUPTED:
        raise HTTPException(409, f"Session not interrupted (status={session.status})")
    if _notifier and session.slack_message_ts:
        _notifier.update_hitl_resolved(session.slack_message_ts, approved=False, result=req.reason)
    _do_reject(session, req.reason, asyncio.get_event_loop())
    return {"session_id": session.id, "status": "running"}


@app.post("/api/edit")
async def edit(req: EditRequest):
    """Edit the proposed action arguments then resume."""
    session = _get_session(req.session_id)
    if session.status != SessionStatus.INTERRUPTED:
        raise HTTPException(409, f"Session not interrupted (status={session.status})")
    session.status = SessionStatus.RUNNING
    session.event_queue = asyncio.Queue()
    loop = asyncio.get_event_loop()
    loop.run_in_executor(
        _executor,
        _resume_agent_sync,
        _agent,
        Command(resume={"decisions": [{"type": "edit", "args": req.args}]}),
        {"configurable": {"thread_id": session.thread_id}},
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
  <button onclick="triggerCheck()" style="margin-left:auto;background:#2d3748;color:#a0aec0;border:1px solid #4a5568;border-radius:6px;padding:4px 12px;font-size:12px;cursor:pointer">▶ Trigger Check Now</button>
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

fetch('/health').then(r=>r.json()).then(d=>{
  document.getElementById('slack-badge').textContent = 'Slack: ' + (d.slack_enabled ? 'on' : 'off');
  document.getElementById('slack-badge').style.color = d.slack_enabled ? '#68d391' : '#fc8181';
});

function appendMsg(text, cls) {
  const chat = document.getElementById('chat');
  const div = document.createElement('div');
  div.className = 'msg ' + cls;
  div.textContent = text;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return div;
}
function setStatus(s) { document.getElementById('status-badge').textContent = s; }
function setInputEnabled(v) {
  document.getElementById('send-btn').disabled = !v;
  document.getElementById('msg-input').disabled = !v;
}
async function sendMessage() {
  const input = document.getElementById('msg-input');
  const text = input.value.trim(); if (!text) return;
  input.value = '';
  appendMsg(text, 'user');
  setInputEnabled(false); setStatus('Running...');
  const typingMsg = appendMsg('Analyzing...', 'bot typing');
  const res = await fetch('/api/chat', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({message: text, session_id: sessionId}),
  });
  const data = await res.json();
  sessionId = data.session_id;
  typingMsg.remove();
  listenForEvents();
}
function listenForEvents() {
  if (eventSource) eventSource.close();
  eventSource = new EventSource('/api/sessions/' + sessionId + '/events');
  eventSource.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.type === 'done') {
      appendMsg(ev.data, 'bot'); setStatus('Ready'); setInputEnabled(true); eventSource.close();
    } else if (ev.type === 'interrupt') {
      showInterrupt(ev.data); setStatus('Approval Required'); eventSource.close();
    } else if (ev.type === 'error') {
      appendMsg('Error: ' + ev.data, 'msg error'); setStatus('Error'); setInputEnabled(true); eventSource.close();
    } else if (ev.type === 'todos') {
      showTodos(ev.data);
    }
  };
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
  const typingMsg = appendMsg('Continuing...', 'bot typing');
  const body = decision === 'reject' ? {session_id: sessionId, reason: reason||''} : {session_id: sessionId};
  await fetch('/api/' + decision, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  typingMsg.remove(); listenForEvents();
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
  appendMsg('Triggering immediate health check...', 'bot');
  await fetch('/api/trigger-check', {method:'POST'});
  appendMsg('Health check started. Results will appear in Slack.', 'bot');
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
