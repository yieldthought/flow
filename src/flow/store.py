"""SQLite store for the runtime."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .common import DEFAULT_MODE, DEFAULT_THINKING, format_utc, parse_utc, utc_now
from .flowfile import FlowSpec
from .paths import db_path, ensure_home

SCHEMA_VERSION = 5


def connect(path: Path | None = None) -> sqlite3.Connection:
    ensure_home()
    target = path or db_path()
    conn = sqlite3.connect(target, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS flow_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            source_path TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS agents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            flow_snapshot_id INTEGER NOT NULL REFERENCES flow_snapshots(id),
            flow_name TEXT NOT NULL,
            source_path TEXT NOT NULL,
            backend TEXT NOT NULL,
            start_state TEXT NOT NULL,
            current_state TEXT NOT NULL,
            substate TEXT NOT NULL,
            phase TEXT NOT NULL,
            cwd TEXT NOT NULL,
            mode TEXT NOT NULL,
            thinking TEXT NOT NULL,
            args_json TEXT NOT NULL,
            tmux_session TEXT NOT NULL,
            thread_id TEXT NOT NULL DEFAULT '',
            rollout_path TEXT NOT NULL DEFAULT '',
            launch_marker TEXT NOT NULL DEFAULT '',
            launch_command TEXT NOT NULL DEFAULT '',
            desired_mode TEXT NOT NULL DEFAULT '',
            desired_thinking TEXT NOT NULL DEFAULT '',
            current_turn_id TEXT NOT NULL DEFAULT '',
            current_turn_kind TEXT NOT NULL DEFAULT '',
            current_turn_started_at TEXT NOT NULL DEFAULT '',
            last_prompt_sent_at TEXT NOT NULL DEFAULT '',
            status_message TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            state_entered_at TEXT NOT NULL,
            ready_at TEXT NOT NULL DEFAULT '',
            ended_at TEXT NOT NULL DEFAULT '',
            pending_state_json TEXT NOT NULL DEFAULT '',
            shutdown_mode TEXT NOT NULL DEFAULT '',
            delete_requested_at TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS commands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            processed_at TEXT NOT NULL DEFAULT '',
            error_text TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS state_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
            state_name TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS transitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
            from_state TEXT NOT NULL,
            to_state TEXT NOT NULL,
            choice TEXT NOT NULL,
            reason TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS agent_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            kind TEXT NOT NULL,
            state_name TEXT NOT NULL DEFAULT '',
            from_state TEXT NOT NULL DEFAULT '',
            to_state TEXT NOT NULL DEFAULT '',
            choice TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS daemon_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            level TEXT NOT NULL,
            message TEXT NOT NULL,
            details_text TEXT NOT NULL DEFAULT ''
        );
        """
    )
    _ensure_meta_defaults(conn)
    _migrate_schema(conn)
    conn.commit()


def ensure_meta_defaults(conn: sqlite3.Connection) -> None:
    _ensure_meta_defaults(conn)


def _ensure_meta_defaults(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('daemon_pid', '')")
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('daemon_started_at', '')")
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('daemon_heartbeat_at', '')")
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('daemon_last_exit_at', '')")
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('daemon_last_exit_kind', '')")
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('daemon_last_error', '')")
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('daemon_last_error_at', '')")
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('list_last_seen_error_at', '')")


def _migrate_schema(conn: sqlite3.Connection) -> None:
    columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(agents)")}
    if "ready_at" not in columns:
        conn.execute("ALTER TABLE agents ADD COLUMN ready_at TEXT NOT NULL DEFAULT ''")
    set_meta(conn, "schema_version", str(SCHEMA_VERSION))


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    conn.commit()


def record_flow_snapshot(conn: sqlite3.Connection, flow: FlowSpec, snapshot_json: str) -> int:
    now = format_utc(utc_now())
    cur = conn.execute(
        "INSERT INTO flow_snapshots(name, source_path, snapshot_json, created_at) VALUES(?, ?, ?, ?)",
        (flow.name, flow.source_path, snapshot_json, now),
    )
    return int(cur.lastrowid)


def create_agent(
    conn: sqlite3.Connection,
    *,
    flow_snapshot_id: int,
    flow_name: str,
    source_path: str,
    backend: str,
    start_state: str,
    cwd: str,
    mode: str,
    thinking: str,
    args_json: str,
) -> int:
    now = format_utc(utc_now())
    cur = conn.execute(
        """
        INSERT INTO agents(
            flow_snapshot_id, flow_name, source_path, backend, start_state, current_state, substate, phase,
            cwd, mode, thinking, args_json, tmux_session, desired_mode, desired_thinking, created_at,
            updated_at, state_entered_at
        )
        VALUES(?, ?, ?, ?, ?, ?, 'normal', 'enter_state', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            flow_snapshot_id,
            flow_name,
            source_path,
            backend,
            start_state,
            start_state,
            cwd,
            mode or DEFAULT_MODE,
            thinking or DEFAULT_THINKING,
            args_json,
            f"flow-agent-PLACEHOLDER",
            mode or DEFAULT_MODE,
            thinking or DEFAULT_THINKING,
            now,
            now,
            now,
        ),
    )
    agent_id = int(cur.lastrowid)
    tmux_name = _tmux_session_name(conn, agent_id)
    conn.execute("UPDATE agents SET tmux_session=? WHERE id=?", (tmux_name, agent_id))
    conn.execute(
        "INSERT INTO state_runs(agent_id, state_name, started_at) VALUES(?, ?, ?)",
        (agent_id, start_state, now),
    )
    record_agent_event(conn, agent_id, "started", created_at=now, state_name=start_state, reason="Agent created")
    return agent_id


def list_agents(conn: sqlite3.Connection, flow_name: str | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM agents WHERE delete_requested_at = ''"
    params: list[Any] = []
    if flow_name:
        sql += " AND flow_name = ?"
        params.append(flow_name)
    sql += " ORDER BY flow_name, current_state, id"
    return list(conn.execute(sql, params))


def get_agent(conn: sqlite3.Connection, agent_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()


def get_flow_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM flow_snapshots WHERE id = ?", (snapshot_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown flow snapshot {snapshot_id}")
    return row


def update_agent(conn: sqlite3.Connection, agent_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = format_utc(utc_now())
    columns = ", ".join(f"{name}=?" for name in fields)
    params = list(fields.values()) + [agent_id]
    conn.execute(f"UPDATE agents SET {columns} WHERE id=?", params)


def enqueue_command(conn: sqlite3.Connection, agent_id: int, kind: str, payload: dict[str, Any] | None = None) -> int:
    now = format_utc(utc_now())
    cur = conn.execute(
        "INSERT INTO commands(agent_id, kind, payload_json, created_at) VALUES(?, ?, ?, ?)",
        (agent_id, kind, json.dumps(payload or {}, sort_keys=True), now),
    )
    return int(cur.lastrowid)


def pending_commands(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM commands WHERE processed_at = '' ORDER BY id"))


def mark_command_processed(conn: sqlite3.Connection, command_id: int, error_text: str = "") -> None:
    conn.execute(
        "UPDATE commands SET processed_at=?, error_text=? WHERE id=?",
        (format_utc(utc_now()), error_text, command_id),
    )


def latest_open_state_run(conn: sqlite3.Connection, agent_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM state_runs WHERE agent_id=? AND ended_at='' ORDER BY id DESC LIMIT 1",
        (agent_id,),
    ).fetchone()


def close_open_state_run(conn: sqlite3.Connection, agent_id: int, ended_at: str | None = None) -> None:
    ended = ended_at or format_utc(utc_now())
    conn.execute(
        "UPDATE state_runs SET ended_at=? WHERE agent_id=? AND ended_at=''",
        (ended, agent_id),
    )


def open_state_run(conn: sqlite3.Connection, agent_id: int, state_name: str, started_at: str | None = None) -> None:
    conn.execute(
        "INSERT INTO state_runs(agent_id, state_name, started_at) VALUES(?, ?, ?)",
        (agent_id, state_name, started_at or format_utc(utc_now())),
    )


def total_active_seconds(conn: sqlite3.Connection, agent_id: int) -> float:
    total = 0.0
    for row in conn.execute("SELECT started_at, ended_at FROM state_runs WHERE agent_id=?", (agent_id,)):
        end = row["ended_at"] or format_utc(utc_now())
        start = parse_utc(row["started_at"])
        finish = parse_utc(end)
        if start is not None and finish is not None:
            total += max(0.0, (finish - start).total_seconds())
    return total


def state_active_seconds(conn: sqlite3.Connection, agent_id: int, state_name: str) -> float:
    total = 0.0
    for row in conn.execute(
        "SELECT started_at, ended_at FROM state_runs WHERE agent_id=? AND state_name=?",
        (agent_id, state_name),
    ):
        end = row["ended_at"] or format_utc(utc_now())
        start = parse_utc(row["started_at"])
        finish = parse_utc(end)
        if start is not None and finish is not None:
            total += max(0.0, (finish - start).total_seconds())
    return total


def record_transition(
    conn: sqlite3.Connection,
    agent_id: int,
    from_state: str,
    to_state: str,
    choice: str,
    reason: str,
    raw_json: str,
) -> None:
    conn.execute(
        """
        INSERT INTO transitions(agent_id, from_state, to_state, choice, reason, raw_json, created_at)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (agent_id, from_state, to_state, choice, reason, raw_json, format_utc(utc_now())),
    )


def record_agent_event(
    conn: sqlite3.Connection,
    agent_id: int,
    kind: str,
    *,
    created_at: str | None = None,
    state_name: str = "",
    from_state: str = "",
    to_state: str = "",
    choice: str = "",
    reason: str = "",
    payload: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO agent_events(agent_id, created_at, kind, state_name, from_state, to_state, choice, reason, payload_json)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            agent_id,
            created_at or format_utc(utc_now()),
            kind,
            state_name,
            from_state,
            to_state,
            choice,
            reason,
            json.dumps(payload or {}, sort_keys=True),
        ),
    )


def list_agent_events(conn: sqlite3.Connection, agent_id: int) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM agent_events WHERE agent_id=? ORDER BY created_at, id", (agent_id,)))


def active_agent_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS count FROM agents WHERE ended_at='' AND delete_requested_at=''").fetchone()
    return int(row["count"]) if row else 0


def total_agent_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS count FROM agents WHERE delete_requested_at=''").fetchone()
    return int(row["count"]) if row else 0


def cumulative_agent_seconds(conn: sqlite3.Connection) -> float:
    return sum(total_active_seconds(conn, int(row["id"])) for row in conn.execute("SELECT id FROM agents WHERE delete_requested_at=''"))


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    return str(row["value"])


def daemon_status(conn: sqlite3.Connection) -> dict[str, str]:
    pid_text = get_meta(conn, "daemon_pid")
    started_at = get_meta(conn, "daemon_started_at")
    heartbeat_at = get_meta(conn, "daemon_heartbeat_at")
    active = False
    if pid_text.isdigit():
        try:
            os.kill(int(pid_text), 0)
        except OSError:
            active = False
        else:
            active = True
    return {
        "active": "1" if active else "0",
        "pid": pid_text,
        "started_at": started_at,
        "heartbeat_at": heartbeat_at,
    }


def daemon_exit_info(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        "last_exit_at": get_meta(conn, "daemon_last_exit_at"),
        "last_exit_kind": get_meta(conn, "daemon_last_exit_kind"),
        "last_error": get_meta(conn, "daemon_last_error"),
        "last_error_at": get_meta(conn, "daemon_last_error_at"),
    }


def clear_daemon_status(conn: sqlite3.Connection) -> None:
    for key in ("daemon_pid", "daemon_started_at", "daemon_heartbeat_at"):
        set_meta(conn, key, "")


def set_daemon_status(conn: sqlite3.Connection, pid: int, started_at: str | None = None, heartbeat_at: str | None = None) -> None:
    set_meta(conn, "daemon_pid", str(pid))
    if started_at is not None:
        set_meta(conn, "daemon_started_at", started_at)
    if heartbeat_at is not None:
        set_meta(conn, "daemon_heartbeat_at", heartbeat_at)


def record_daemon_exit(
    conn: sqlite3.Connection,
    *,
    kind: str,
    exited_at: str | None = None,
    error_text: str = "",
) -> None:
    timestamp = exited_at or format_utc(utc_now())
    set_meta(conn, "daemon_last_exit_kind", kind)
    set_meta(conn, "daemon_last_exit_at", timestamp)
    if kind == "error":
        set_meta(conn, "daemon_last_error", error_text)
        set_meta(conn, "daemon_last_error_at", timestamp)


def record_daemon_event(
    conn: sqlite3.Connection,
    *,
    level: str,
    message: str,
    created_at: str | None = None,
    details_text: str = "",
) -> None:
    conn.execute(
        "INSERT INTO daemon_events(created_at, level, message, details_text) VALUES(?, ?, ?, ?)",
        (created_at or format_utc(utc_now()), level, message, details_text),
    )


def list_daemon_events(conn: sqlite3.Connection, since: str = "") -> list[sqlite3.Row]:
    sql = "SELECT * FROM daemon_events"
    params: list[Any] = []
    if since:
        sql += " WHERE created_at > ?"
        params.append(since)
    sql += " ORDER BY created_at, id"
    return list(conn.execute(sql, params))


def list_error_events(conn: sqlite3.Connection, since: str = "") -> list[sqlite3.Row]:
    sql = """
        SELECT
            agent_events.*,
            agents.flow_name AS flow_name,
            agents.current_state AS current_state
        FROM agent_events
        JOIN agents ON agents.id = agent_events.agent_id
        WHERE agent_events.kind IN ('error', 'needs_help')
    """
    params: list[Any] = []
    if since:
        sql += " AND agent_events.created_at > ?"
        params.append(since)
    sql += " ORDER BY agent_events.created_at, agent_events.id"
    return list(conn.execute(sql, params))


def _tmux_session_name(conn: sqlite3.Connection, agent_id: int) -> str:
    runtime_hash = hashlib.sha1(str(_database_file(conn)).encode("utf-8")).hexdigest()[:8]
    return f"flow-{runtime_hash}-agent-{agent_id}"


def _database_file(conn: sqlite3.Connection) -> Path:
    row = conn.execute("PRAGMA database_list").fetchone()
    if row is None or not row[2]:
        return db_path()
    return Path(str(row[2]))
