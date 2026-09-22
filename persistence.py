"""Durable state: langgraph checkpoints, sessions, HITL audit, monitoring state.

Everything the bot needed to survive a restart used to live in process memory —
``MemorySaver``, ``InMemoryStore``, and ``api._sessions``. A pod replacement
orphaned every pending HITL approval: the Slack Approve button stayed live but
the session behind it was gone, so the click dead-ended and the proposed cluster
change could neither be applied nor properly rejected.

This module backs all of that with Postgres.

Degradation is deliberate. If ``DATABASE_URL`` is unset (local CLI use) or the
database is unreachable at startup, the process falls back to in-memory
equivalents and logs loudly rather than refusing to boot. The bot runs *inside*
the cluster hosting its own database, so a cluster problem must not also take
away the operator's ability to ask the bot about it. ``/health`` reports which
mode is live so the degradation is never silent.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from monitor_state import ReportDiff, StoredFinding

log = logging.getLogger("sre-agent.persistence")


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id                text PRIMARY KEY,
    thread_id         text NOT NULL,
    status            text NOT NULL,
    source            text NOT NULL DEFAULT 'api',
    pending_decisions integer NOT NULL DEFAULT 1,
    pending_actions   jsonb,
    interrupt_data    jsonb,
    last_response     text,
    slack_message_ts  text,
    slack_channel     text,
    slack_thread_ts   text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

-- Append-only. Nothing in this module issues UPDATE or DELETE against it: the
-- point is an immutable record of who authorised each cluster mutation.
CREATE TABLE IF NOT EXISTS hitl_audit (
    id         bigserial PRIMARY KEY,
    ts         timestamptz NOT NULL DEFAULT now(),
    session_id text NOT NULL,
    thread_id  text,
    actor      text,
    actor_id   text,
    decision   text NOT NULL,
    source     text,
    tool_name  text,
    tool_args  jsonb,
    result     text
);
CREATE INDEX IF NOT EXISTS hitl_audit_session_idx ON hitl_audit (session_id, ts DESC);
CREATE INDEX IF NOT EXISTS hitl_audit_ts_idx ON hitl_audit (ts DESC);

CREATE TABLE IF NOT EXISTS finding_state (
    fingerprint   text PRIMARY KEY,
    namespace     text NOT NULL DEFAULT '',
    kind          text NOT NULL DEFAULT '',
    resource_name text NOT NULL DEFAULT '',
    reason        text NOT NULL DEFAULT '',
    severity      text NOT NULL,
    title         text NOT NULL DEFAULT '',
    detail        text NOT NULL DEFAULT '',
    first_seen    timestamptz NOT NULL DEFAULT now(),
    last_seen     timestamptz NOT NULL DEFAULT now(),
    times_seen    integer NOT NULL DEFAULT 1,
    resolved_at   timestamptz,
    ack_until     timestamptz
);
CREATE INDEX IF NOT EXISTS finding_state_open_idx
    ON finding_state (last_seen DESC) WHERE resolved_at IS NULL;

-- Maps a posted report to the fingerprints it covered, so the Slack "Ack"
-- button can carry a short opaque id instead of a fingerprint list that would
-- blow past Slack's 2000-character button value limit.
CREATE TABLE IF NOT EXISTS monitor_reports (
    report_id    text PRIMARY KEY,
    created_at   timestamptz NOT NULL DEFAULT now(),
    fingerprints jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS monitor_meta (
    key   text PRIMARY KEY,
    value text
);

-- Correlates a tracked finding (by fingerprint) with a write-tool call that
-- was approved while it was open and that it did not outlive. A fingerprint
-- can have more than one distinct known fix over its history (e.g. sometimes
-- a scale fixed it, sometimes a restart did), so the PK includes tool_name.
CREATE TABLE IF NOT EXISTS resolution_memory (
    fingerprint        text NOT NULL,
    tool_name          text NOT NULL,
    tool_args          jsonb NOT NULL,
    first_confirmed_at timestamptz NOT NULL DEFAULT now(),
    last_confirmed_at  timestamptz NOT NULL DEFAULT now(),
    times_confirmed    integer NOT NULL DEFAULT 1,
    last_actor         text NOT NULL DEFAULT '',
    PRIMARY KEY (fingerprint, tool_name)
);
CREATE INDEX IF NOT EXISTS resolution_memory_fingerprint_idx ON resolution_memory (fingerprint);
"""

# How far back a resolved finding is still remembered. Keeps flap detection
# working ("this came back for the 4th time this week") while bounding the table.
_RESOLVED_RETENTION_DAYS = 7


class NullDatabase:
    """No-op implementation used when Postgres is unavailable.

    Same surface as :class:`PostgresDatabase` so callers never branch on
    ``if db:``. Monitoring degrades to its old stateless behaviour (every run
    looks new) and the audit log is dropped — hence the startup warning.
    """

    kind = "memory"
    available = False

    def setup(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    def save_session(self, session: dict) -> None:
        pass

    def load_session(self, session_id: str) -> Optional[dict]:
        return None

    def record_decision(self, **kwargs) -> None:
        log.info(
            "[NO AUDIT DB] HITL %s on session=%s by actor=%s tool=%s",
            kwargs.get("decision"), kwargs.get("session_id"),
            kwargs.get("actor"), kwargs.get("tool_name"),
        )

    def recent_decisions(self, limit: int = 50) -> list[dict]:
        return []

    def load_tracked_findings(self) -> dict[str, StoredFinding]:
        return {}

    def apply_diff(self, diff: ReportDiff, now: Optional[datetime] = None) -> None:
        pass

    def save_report(self, fingerprints: list[str]) -> Optional[str]:
        return None

    def ack_report(self, report_id: str, hours: int) -> int:
        return 0

    def next_check_number(self) -> int:
        return 0

    def decisions_since(self, since: datetime, limit: int = 500) -> list[dict]:
        return []

    def record_resolution(
        self, fingerprint: str, tool_name: str, tool_args: Any, actor: str = ""
    ) -> None:
        pass

    def find_known_fixes(self, fingerprint: str) -> list[dict]:
        return []


class PostgresDatabase:
    """Postgres-backed state. All SQL is parameterized; no value interpolation."""

    kind = "postgres"
    available = True

    def __init__(self, pool):
        self._pool = pool

    def setup(self) -> None:
        with self._pool.connection() as conn:
            conn.execute(SCHEMA)
        log.info(
            "Postgres schema ready (sessions, hitl_audit, finding_state, "
            "monitor_*, resolution_memory)"
        )

    def close(self) -> None:
        try:
            self._pool.close()
        except Exception:  # pragma: no cover - shutdown best effort
            log.debug("Connection pool close failed", exc_info=True)

    # -- sessions ---------------------------------------------------------

    def save_session(self, session: dict) -> None:
        """Write-through upsert of a session's durable fields."""
        from psycopg.types.json import Jsonb

        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    id, thread_id, status, source, pending_decisions,
                    pending_actions, interrupt_data, last_response,
                    slack_message_ts, slack_channel, slack_thread_ts, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (id) DO UPDATE SET
                    thread_id        = EXCLUDED.thread_id,
                    status           = EXCLUDED.status,
                    source           = EXCLUDED.source,
                    pending_decisions= EXCLUDED.pending_decisions,
                    pending_actions  = EXCLUDED.pending_actions,
                    interrupt_data   = EXCLUDED.interrupt_data,
                    last_response    = EXCLUDED.last_response,
                    slack_message_ts = EXCLUDED.slack_message_ts,
                    slack_channel    = EXCLUDED.slack_channel,
                    slack_thread_ts  = EXCLUDED.slack_thread_ts,
                    updated_at       = now()
                """,
                (
                    session["id"],
                    session["thread_id"],
                    session["status"],
                    session.get("source", "api"),
                    session.get("pending_decisions", 1),
                    Jsonb(session.get("pending_actions") or []),
                    Jsonb(session.get("interrupt_data") or []),
                    session.get("last_response", ""),
                    session.get("slack_message_ts"),
                    session.get("slack_channel"),
                    session.get("slack_thread_ts"),
                ),
            )

    def load_session(self, session_id: str) -> Optional[dict]:
        with self._pool.connection() as conn:
            cur = conn.execute(
                """
                SELECT id, thread_id, status, source, pending_decisions,
                       pending_actions, interrupt_data, last_response,
                       slack_message_ts, slack_channel, slack_thread_ts
                  FROM sessions
                 WHERE id = %s
                """,
                (session_id,),
            )
            return cur.fetchone()

    # -- audit ------------------------------------------------------------

    def record_decision(
        self,
        session_id: str,
        decision: str,
        thread_id: str = "",
        actor: str = "",
        actor_id: str = "",
        source: str = "",
        tool_name: str = "",
        tool_args: Any = None,
        result: str = "",
    ) -> None:
        """Append one immutable HITL decision record."""
        from psycopg.types.json import Jsonb

        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO hitl_audit (
                    session_id, thread_id, actor, actor_id, decision,
                    source, tool_name, tool_args, result
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    session_id, thread_id, actor, actor_id, decision,
                    source, tool_name, Jsonb(tool_args or {}), (result or "")[:4000],
                ),
            )

    def recent_decisions(self, limit: int = 50) -> list[dict]:
        with self._pool.connection() as conn:
            cur = conn.execute(
                """
                SELECT ts, session_id, actor, actor_id, decision, source,
                       tool_name, tool_args, result
                  FROM hitl_audit
                 ORDER BY ts DESC
                 LIMIT %s
                """,
                (min(max(limit, 1), 500),),
            )
            return list(cur.fetchall())

    def decisions_since(self, since: datetime, limit: int = 500) -> list[dict]:
        """Approved/edited write-tool calls at or after ``since``, newest first.

        Scoped by time rather than count (unlike ``recent_decisions``) because a
        finding can stay open for days, and a global 500-row cap could miss the
        approval that fixed it in a busy cluster.
        """
        with self._pool.connection() as conn:
            cur = conn.execute(
                """
                SELECT ts, tool_name, tool_args, actor
                  FROM hitl_audit
                 WHERE ts >= %s
                   AND decision IN ('approve', 'edit')
                   AND tool_name != ''
                 ORDER BY ts DESC
                 LIMIT %s
                """,
                (since, min(max(limit, 1), 500)),
            )
            return list(cur.fetchall())

    # -- monitoring finding state -----------------------------------------

    def load_tracked_findings(self) -> dict[str, StoredFinding]:
        """Open findings, plus recently-resolved and acked ones.

        Recently-resolved rows are included so a returning problem is reported
        as new again while keeping its cumulative ``times_seen``.
        """
        with self._pool.connection() as conn:
            cur = conn.execute(
                """
                SELECT fingerprint, severity, title, namespace, first_seen,
                       last_seen, times_seen, resolved_at, ack_until
                  FROM finding_state
                 WHERE resolved_at IS NULL
                    OR resolved_at > now() - make_interval(days => %s)
                    OR ack_until > now()
                """,
                (_RESOLVED_RETENTION_DAYS,),
            )
            rows = cur.fetchall()

        return {
            r["fingerprint"]: StoredFinding(
                fingerprint=r["fingerprint"],
                severity=r["severity"],
                title=r["title"],
                namespace=r["namespace"],
                first_seen=r["first_seen"],
                last_seen=r["last_seen"],
                times_seen=r["times_seen"],
                resolved_at=r["resolved_at"],
                ack_until=r["ack_until"],
            )
            for r in rows
        }

    def apply_diff(self, diff: ReportDiff, now: Optional[datetime] = None) -> None:
        """Persist the outcome of one monitoring run, atomically."""
        now = now or datetime.now(timezone.utc)
        seen = diff.active + diff.suppressed

        with self._pool.connection() as conn:
            with conn.transaction():
                for delta in seen:
                    f = delta.finding
                    conn.execute(
                        """
                        INSERT INTO finding_state (
                            fingerprint, namespace, kind, resource_name, reason,
                            severity, title, detail, first_seen, last_seen,
                            times_seen, resolved_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)
                        ON CONFLICT (fingerprint) DO UPDATE SET
                            namespace     = EXCLUDED.namespace,
                            kind          = EXCLUDED.kind,
                            resource_name = EXCLUDED.resource_name,
                            reason        = EXCLUDED.reason,
                            severity      = EXCLUDED.severity,
                            title         = EXCLUDED.title,
                            detail        = EXCLUDED.detail,
                            last_seen     = EXCLUDED.last_seen,
                            times_seen    = EXCLUDED.times_seen,
                            resolved_at   = NULL,
                            -- A finding that had been resolved and came back
                            -- restarts its clock; one that never closed keeps
                            -- the earliest first_seen we know about.
                            first_seen    = CASE
                                WHEN finding_state.resolved_at IS NOT NULL
                                    THEN EXCLUDED.first_seen
                                ELSE LEAST(finding_state.first_seen, EXCLUDED.first_seen)
                            END
                        """,
                        (
                            delta.fingerprint,
                            getattr(f, "namespace", "") or "",
                            getattr(f, "kind", "") or "",
                            getattr(f, "resource_name", "") or "",
                            getattr(f, "reason", "") or "",
                            f.severity,
                            (getattr(f, "title", "") or "")[:500],
                            (getattr(f, "detail", "") or "")[:4000],
                            delta.first_seen,
                            now,
                            delta.times_seen,
                        ),
                    )

                if diff.resolved:
                    conn.execute(
                        """
                        UPDATE finding_state
                           SET resolved_at = %s
                         WHERE fingerprint = ANY(%s)
                        """,
                        (now, [r.fingerprint for r in diff.resolved]),
                    )

    def save_report(self, fingerprints: list[str]) -> Optional[str]:
        """Record which fingerprints a posted report covered; return its id."""
        from psycopg.types.json import Jsonb

        report_id = uuid.uuid4().hex[:12]
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO monitor_reports (report_id, fingerprints) VALUES (%s, %s)",
                (report_id, Jsonb(list(fingerprints))),
            )
        return report_id

    def ack_report(self, report_id: str, hours: int) -> int:
        """Suppress every finding in a report for ``hours``. Returns rows acked."""
        until = datetime.now(timezone.utc) + timedelta(hours=hours)
        with self._pool.connection() as conn:
            cur = conn.execute(
                "SELECT fingerprints FROM monitor_reports WHERE report_id = %s",
                (report_id,),
            )
            row = cur.fetchone()
            if not row:
                return 0
            fingerprints = list(row["fingerprints"] or [])
            if not fingerprints:
                return 0
            cur = conn.execute(
                """
                UPDATE finding_state
                   SET ack_until = %s
                 WHERE fingerprint = ANY(%s)
                   AND resolved_at IS NULL
                """,
                (until, fingerprints),
            )
            return cur.rowcount

    # -- meta -------------------------------------------------------------

    def next_check_number(self) -> int:
        """Monotonic counter of completed checks, used to pace the digest."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO monitor_meta (key, value) VALUES ('check_count', '1')
                ON CONFLICT (key) DO UPDATE
                    SET value = (COALESCE(monitor_meta.value::bigint, 0) + 1)::text
                RETURNING value
                """
            )
            row = cur.fetchone()
        try:
            return int(row["value"])
        except (TypeError, ValueError, KeyError):
            return 0

    # -- resolution memory --------------------------------------------------

    def record_resolution(
        self, fingerprint: str, tool_name: str, tool_args: Any, actor: str = ""
    ) -> None:
        """Remember that ``tool_name``/``tool_args`` resolved ``fingerprint``.

        Upserts on (fingerprint, tool_name): a repeat pairing increments
        ``times_confirmed`` instead of creating a duplicate row, so confidence
        in a known fix grows the more times it is independently observed to work.
        """
        from psycopg.types.json import Jsonb

        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO resolution_memory (
                    fingerprint, tool_name, tool_args, last_actor
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (fingerprint, tool_name) DO UPDATE SET
                    tool_args         = EXCLUDED.tool_args,
                    last_confirmed_at = now(),
                    times_confirmed   = resolution_memory.times_confirmed + 1,
                    last_actor        = EXCLUDED.last_actor
                """,
                (fingerprint, tool_name, Jsonb(tool_args or {}), actor or ""),
            )

    def find_known_fixes(self, fingerprint: str) -> list[dict]:
        """Known fixes for a fingerprint, most-confirmed and most-recent first."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                """
                SELECT tool_name, tool_args, times_confirmed,
                       first_confirmed_at, last_confirmed_at, last_actor
                  FROM resolution_memory
                 WHERE fingerprint = %s
                 ORDER BY times_confirmed DESC, last_confirmed_at DESC
                """,
                (fingerprint,),
            )
            return list(cur.fetchall())


def init_persistence(database_url: str = "") -> tuple[Any, Any, Any]:
    """Build ``(checkpointer, store, database)``.

    Falls back to in-memory implementations when no DSN is configured or the
    database cannot be reached, so the CLI and a degraded cluster both still work.
    """
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.store.memory import InMemoryStore

    if not database_url:
        log.warning(
            "DATABASE_URL not set — using in-memory checkpointer/store. Pending HITL "
            "approvals and monitoring finding-state will NOT survive a restart."
        )
        return MemorySaver(), InMemoryStore(), NullDatabase()

    pool = None
    try:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
        from langgraph.checkpoint.postgres import PostgresSaver
        from langgraph.store.postgres import PostgresStore

        pool = ConnectionPool(
            conninfo=database_url,
            min_size=1,
            max_size=10,
            open=True,
            # PostgresSaver requires both of these on every connection.
            kwargs={"autocommit": True, "row_factory": dict_row},
        )

        checkpointer = PostgresSaver(pool)
        checkpointer.setup()

        store = PostgresStore(pool)
        store.setup()

        db = PostgresDatabase(pool)
        db.setup()

        log.info("Persistence ready (postgres)")
        return checkpointer, store, db
    except Exception:
        log.exception(
            "Postgres unavailable — DEGRADED: falling back to in-memory state. "
            "HITL approvals will not survive a restart and no audit trail is being written."
        )
        # An opened-but-unusable pool keeps background worker threads retrying
        # the dead DSN for the life of the process. Close it before degrading.
        if pool is not None:
            try:
                pool.close()
            except Exception:
                log.debug("Failed to close half-open pool", exc_info=True)
        return MemorySaver(), InMemoryStore(), NullDatabase()
