from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from flow.ansi import PALETTE
from flow.cli import cmd_list, cmd_restart, cmd_validate, cmd_view, main, run_top_mode
from flow.common import format_utc, utc_now
from flow.render import fit_list_top, fit_show_top, render_list, render_show
from flow.store import connect, create_agent, get_agent, get_meta, init_db, record_agent_event, record_daemon_event, record_flow_snapshot, set_meta
from flow.flowfile import flow_to_dict, load_flow, parse_start_arguments, render_flow


def write_flow(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_cmd_validate_success(tmp_path: Path, capsys: object) -> None:
    conn = connect(tmp_path / "runtime.sqlite3")
    init_db(conn)
    path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: .

start:
  start: true
  prompt: hi
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    assert cmd_validate(conn, str(path)) == 0
    out = capsys.readouterr().out
    assert "valid" in out


def test_cmd_validate_multiple_files(tmp_path: Path, capsys: object) -> None:
    conn = connect(tmp_path / "runtime.sqlite3")
    init_db(conn)
    good = write_flow(
        tmp_path / "good.yaml",
        """
flow:
  name: demo-good
  version: 1
  path: .

start:
  start: true
  prompt: hi
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    bad = write_flow(
        tmp_path / "bad.yaml",
        """
flow:
  name: demo-bad
  version: 1

start:
  start: true
  prompt: hi
  transitions:
    - go: missing
""".strip(),
    )

    assert cmd_validate(conn, [str(good), str(bad)]) == 1
    captured = capsys.readouterr()

    assert f"{good}:" in captured.out
    assert "flow file is valid" in captured.out
    assert f"{bad}:" in captured.err
    assert "missing" in captured.err


def test_parse_start_arguments_help_uses_path_metavar(tmp_path: Path, capsys: object) -> None:
    path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1

start:
  start: true
  prompt: hi
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )

    with pytest.raises(SystemExit):
        parse_start_arguments(load_flow(path), None, ["--help"])

    out = capsys.readouterr().out
    assert "--path PATH" in out
    assert "__PATH__" not in out


def test_main_list_migrates_legacy_db_without_daemon_events(tmp_path: Path, monkeypatch: object, capsys: object) -> None:
    flow_home = tmp_path / ".flow"
    flow_home.mkdir()
    legacy_db = flow_home / "runtime.sqlite3"
    conn = sqlite3.connect(legacy_db)
    conn.executescript(
        """
        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE agents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            flow_snapshot_id INTEGER NOT NULL,
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
        CREATE TABLE state_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id INTEGER NOT NULL,
            state_name TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL DEFAULT ''
        );
        """
    )
    conn.execute(
        """
        INSERT INTO agents(
            flow_snapshot_id, flow_name, source_path, backend, start_state, current_state, substate, phase,
            cwd, mode, thinking, args_json, tmux_session, created_at, updated_at, state_entered_at
        )
        VALUES(1, 'demo', '/tmp/flow.yaml', 'fake', 'start', 'start', 'normal', 'enter_state',
               '/tmp/work', 'yolo', 'xhigh', '{}', 'flow-test-agent-1', '2026-04-01T12:00:00Z',
               '2026-04-01T12:00:00Z', '2026-04-01T12:00:00Z')
        """
    )
    conn.execute(
        "INSERT INTO state_runs(agent_id, state_name, started_at) VALUES(1, 'start', '2026-04-01T12:00:00Z')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("FLOW_HOME", str(flow_home))

    assert main(["list"]) == 0

    out = capsys.readouterr().out
    assert "Runtime" in out
    assert "demo" in out

    migrated = sqlite3.connect(legacy_db)
    tables = {row[0] for row in migrated.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    migrated.close()
    assert "daemon_events" in tables


def test_cmd_view_single_agent_uses_direct_attach(tmp_path: Path, monkeypatch: object) -> None:
    conn = connect(tmp_path / "runtime.sqlite3")
    init_db(conn)
    path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: .

start:
  start: true
  prompt: hi
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    flow = render_flow(load_flow(path), {}, cwd_override=str(tmp_path))
    snapshot_id = record_flow_snapshot(conn, flow, str(flow_to_dict(flow)))
    agent_id = create_agent(
        conn,
        flow_snapshot_id=snapshot_id,
        flow_name=flow.name,
        source_path=flow.source_path,
        backend="fake",
        start_state="start",
        cwd=str(tmp_path),
        mode="yolo",
        thinking="xhigh",
        args_json="{}",
    )
    conn.commit()

    calls: dict[str, list[int]] = {"attach": [], "attach_many": []}

    class FakeBackend:
        def session_exists(self, agent: object) -> bool:
            return True

        def attach(self, agent: object) -> int:
            calls["attach"].append(int(agent["id"]))
            return 0

        def attach_many(self, agents: object) -> int:
            calls["attach_many"] = [int(agent["id"]) for agent in agents]
            return 0

    monkeypatch.setattr("flow.cli.CodexBackend", FakeBackend)

    assert cmd_view(conn, [str(agent_id)]) == 0
    assert calls["attach"] == [agent_id]
    assert calls["attach_many"] == []


def test_cmd_view_multiple_agents_uses_tiled_attach(tmp_path: Path, monkeypatch: object) -> None:
    conn = connect(tmp_path / "runtime.sqlite3")
    init_db(conn)
    path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: .

start:
  start: true
  prompt: hi
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    flow = render_flow(load_flow(path), {}, cwd_override=str(tmp_path))
    snapshot_id = record_flow_snapshot(conn, flow, str(flow_to_dict(flow)))
    first_id = create_agent(
        conn,
        flow_snapshot_id=snapshot_id,
        flow_name=flow.name,
        source_path=flow.source_path,
        backend="fake",
        start_state="start",
        cwd=str(tmp_path),
        mode="yolo",
        thinking="xhigh",
        args_json="{}",
    )
    second_id = create_agent(
        conn,
        flow_snapshot_id=snapshot_id,
        flow_name=flow.name,
        source_path=flow.source_path,
        backend="fake",
        start_state="start",
        cwd=str(tmp_path),
        mode="yolo",
        thinking="xhigh",
        args_json="{}",
    )
    conn.commit()

    calls: dict[str, list[int]] = {"attach": [], "attach_many": []}

    class FakeBackend:
        def session_exists(self, agent: object) -> bool:
            return True

        def attach(self, agent: object) -> int:
            calls["attach"].append(int(agent["id"]))
            return 0

        def attach_many(self, agents: object) -> int:
            calls["attach_many"] = [int(agent["id"]) for agent in agents]
            return 0

    monkeypatch.setattr("flow.cli.CodexBackend", FakeBackend)

    assert cmd_view(conn, [str(first_id), str(second_id), str(first_id)]) == 0
    assert calls["attach"] == []
    assert calls["attach_many"] == [first_id, second_id]


def test_cmd_view_all_selects_live_agents_only(tmp_path: Path, monkeypatch: object) -> None:
    conn = connect(tmp_path / "runtime.sqlite3")
    init_db(conn)
    path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: .

start:
  start: true
  prompt: hi
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    flow = render_flow(load_flow(path), {}, cwd_override=str(tmp_path))
    snapshot_id = record_flow_snapshot(conn, flow, str(flow_to_dict(flow)))
    live_id = create_agent(
        conn,
        flow_snapshot_id=snapshot_id,
        flow_name=flow.name,
        source_path=flow.source_path,
        backend="fake",
        start_state="start",
        cwd=str(tmp_path),
        mode="yolo",
        thinking="xhigh",
        args_json="{}",
    )
    ended_id = create_agent(
        conn,
        flow_snapshot_id=snapshot_id,
        flow_name=flow.name,
        source_path=flow.source_path,
        backend="fake",
        start_state="start",
        cwd=str(tmp_path),
        mode="yolo",
        thinking="xhigh",
        args_json="{}",
    )
    conn.execute(
        "UPDATE agents SET ended_at=? WHERE id=?",
        (format_utc(utc_now()), ended_id),
    )
    conn.commit()

    calls: dict[str, list[int]] = {"attach": [], "attach_many": []}

    class FakeBackend:
        def session_exists(self, agent: object) -> bool:
            return True

        def attach(self, agent: object) -> int:
            calls["attach"].append(int(agent["id"]))
            return 0

        def attach_many(self, agents: object) -> int:
            calls["attach_many"] = [int(agent["id"]) for agent in agents]
            return 0

    monkeypatch.setattr("flow.cli.CodexBackend", FakeBackend)

    assert cmd_view(conn, [], view_all=True) == 0
    assert calls["attach"] == [live_id]
    assert calls["attach_many"] == []


def test_cmd_view_requires_ids_or_all(tmp_path: Path, capsys: object) -> None:
    conn = connect(tmp_path / "runtime.sqlite3")
    init_db(conn)

    assert cmd_view(conn, []) == 1

    err = capsys.readouterr().err
    assert "specify one or more agent ids or use --all" in err


def test_cmd_restart_shuts_down_then_inits_when_active(tmp_path: Path, monkeypatch: object) -> None:
    conn = connect(tmp_path / "runtime.sqlite3")
    init_db(conn)
    calls: list[str] = []

    monkeypatch.setattr("flow.cli.daemon_status", lambda _conn: {"active": "1", "pid": "123", "started_at": "", "heartbeat_at": ""})

    def fake_shutdown(_conn: object, tokens: list[str]) -> int:
        calls.append(f"shutdown:{tokens}")
        return 0

    def fake_init(_conn: object) -> int:
        calls.append("init")
        return 0

    monkeypatch.setattr("flow.cli.cmd_shutdown", fake_shutdown)
    monkeypatch.setattr("flow.cli.cmd_init", fake_init)

    assert cmd_restart(conn) == 0
    assert calls == ["shutdown:[]", "init"]


def test_cmd_restart_inits_directly_when_inactive(tmp_path: Path, monkeypatch: object) -> None:
    conn = connect(tmp_path / "runtime.sqlite3")
    init_db(conn)
    calls: list[str] = []

    monkeypatch.setattr("flow.cli.daemon_status", lambda _conn: {"active": "0", "pid": "", "started_at": "", "heartbeat_at": ""})

    def fake_shutdown(_conn: object, tokens: list[str]) -> int:
        calls.append(f"shutdown:{tokens}")
        return 0

    def fake_init(_conn: object) -> int:
        calls.append("init")
        return 0

    monkeypatch.setattr("flow.cli.cmd_shutdown", fake_shutdown)
    monkeypatch.setattr("flow.cli.cmd_init", fake_init)

    assert cmd_restart(conn) == 0
    assert calls == ["init"]


def test_cmd_list_top_updates_diagnostics_watermark_on_exit(tmp_path: Path, monkeypatch: object) -> None:
    conn = connect(tmp_path / "runtime.sqlite3")
    init_db(conn)
    path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: .

start:
  start: true
  prompt: hi
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    flow = render_flow(load_flow(path), {}, cwd_override=str(tmp_path))
    snapshot_id = record_flow_snapshot(conn, flow, str(flow_to_dict(flow)))
    create_agent(
        conn,
        flow_snapshot_id=snapshot_id,
        flow_name=flow.name,
        source_path=flow.source_path,
        backend="fake",
        start_state="start",
        cwd=str(tmp_path),
        mode="yolo",
        thinking="xhigh",
        args_json="{}",
    )
    conn.commit()

    seen: dict[str, str] = {}

    def fake_top(render_once: object, *, fitter: object, on_exit: object = None, refresh_seconds: float = 5.0) -> int:
        del refresh_seconds
        seen["before"] = get_meta(conn, "list_last_seen_error_at")
        assert fitter is fit_list_top
        assert "Runtime" in render_once()
        assert on_exit is not None
        on_exit()
        seen["after"] = get_meta(conn, "list_last_seen_error_at")
        return 0

    monkeypatch.setattr("flow.cli.run_top_mode", fake_top)

    assert cmd_list(conn, None, top=True) == 0
    assert seen["before"] == ""
    assert seen["after"]


def test_run_top_mode_requires_tty(monkeypatch: object, capsys: object) -> None:
    class FakeInput:
        def isatty(self) -> bool:
            return False

    class FakeOutput:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("flow.cli.sys.stdin", FakeInput())
    monkeypatch.setattr("flow.cli.sys.stdout", FakeOutput())

    assert run_top_mode(lambda: "frame", fitter=fit_list_top) == 1
    assert "--top requires an interactive terminal" in capsys.readouterr().err


def test_render_list_groups_agents(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    conn = connect()
    init_db(conn)
    path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: .

start:
  start: true
  prompt: hi
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    flow = render_flow(load_flow(path), {}, cwd_override=str(tmp_path))
    snapshot_id = record_flow_snapshot(conn, flow, str(flow_to_dict(flow)))
    agent_id = create_agent(
        conn,
        flow_snapshot_id=snapshot_id,
        flow_name=flow.name,
        source_path=flow.source_path,
        backend="fake",
        start_state="start",
        cwd=str(tmp_path),
        mode="yolo",
        thinking="xhigh",
        args_json="{}",
    )
    conn.commit()
    text = render_list(conn, [dict(row) for row in conn.execute("SELECT * FROM agents")])
    assert "Runtime" in text
    assert "demo" in text
    assert f"#{agent_id}" in text
    assert "working 00:00:" in text


def test_fit_list_top_truncates_with_summary() -> None:
    text = "one\ntwo\nthree\nfour"
    fitted = fit_list_top(text, 3)
    lines = fitted.splitlines()
    assert lines[:2] == ["one", "two"]
    assert "2 more lines" in lines[2]


def test_render_list_shows_waiting_agents(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    conn = connect()
    init_db(conn)
    path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: .

start:
  start: true
  prompt: hi
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    flow = render_flow(load_flow(path), {}, cwd_override=str(tmp_path))
    snapshot_id = record_flow_snapshot(conn, flow, str(flow_to_dict(flow)))
    agent_id = create_agent(
        conn,
        flow_snapshot_id=snapshot_id,
        flow_name=flow.name,
        source_path=flow.source_path,
        backend="fake",
        start_state="start",
        cwd=str(tmp_path),
        mode="yolo",
        thinking="xhigh",
        args_json="{}",
    )
    conn.execute(
        "UPDATE agents SET ready_at=?, phase=? WHERE id=?",
        (format_utc(utc_now() + timedelta(minutes=5)), "waiting", agent_id),
    )
    conn.commit()

    text = render_list(conn, [dict(row) for row in conn.execute("SELECT * FROM agents")])
    assert re.search(r"waiting\s+00:0[45]:", text)


def test_render_list_shows_paused_for_interaction_agents_even_with_ready_at(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    conn = connect()
    init_db(conn)
    path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: .

start:
  start: true
  prompt: hi
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    flow = render_flow(load_flow(path), {}, cwd_override=str(tmp_path))
    snapshot_id = record_flow_snapshot(conn, flow, str(flow_to_dict(flow)))
    agent_id = create_agent(
        conn,
        flow_snapshot_id=snapshot_id,
        flow_name=flow.name,
        source_path=flow.source_path,
        backend="fake",
        start_state="start",
        cwd=str(tmp_path),
        mode="yolo",
        thinking="xhigh",
        args_json="{}",
    )
    conn.execute(
        "UPDATE agents SET ready_at=?, substate=?, phase=? WHERE id=?",
        (format_utc(utc_now() + timedelta(minutes=5)), "interaction", "paused", agent_id),
    )
    conn.commit()

    text = render_list(conn, [dict(row) for row in conn.execute("SELECT * FROM agents")])
    assert re.search(r"paused\s+00:00:", text)
    assert not re.search(r"waiting\s+00:0[45]:", text)


def test_render_list_aligns_id_status_and_time_columns(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    conn = connect()
    init_db(conn)
    path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: .

start:
  start: true
  prompt: hi
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    flow = render_flow(load_flow(path), {}, cwd_override=str(tmp_path))
    snapshot_id = record_flow_snapshot(conn, flow, str(flow_to_dict(flow)))
    first_id = create_agent(
        conn,
        flow_snapshot_id=snapshot_id,
        flow_name=flow.name,
        source_path=flow.source_path,
        backend="fake",
        start_state="start",
        cwd=str(tmp_path),
        mode="yolo",
        thinking="xhigh",
        args_json='{"site":"reddit.com/r/locallama"}',
    )
    second_id = create_agent(
        conn,
        flow_snapshot_id=snapshot_id,
        flow_name=flow.name,
        source_path=flow.source_path,
        backend="fake",
        start_state="start",
        cwd=str(tmp_path),
        mode="yolo",
        thinking="xhigh",
        args_json='{"site":"https://karpathy.github.io"}',
    )
    third_id = create_agent(
        conn,
        flow_snapshot_id=snapshot_id,
        flow_name=flow.name,
        source_path=flow.source_path,
        backend="fake",
        start_state="start",
        cwd=str(tmp_path),
        mode="yolo",
        thinking="xhigh",
        args_json='{"site":"news.ycombinator.com"}',
    )
    conn.execute(
        "UPDATE agents SET current_state='check', ready_at=?, phase='waiting' WHERE id=?",
        (format_utc(utc_now() + timedelta(minutes=31)), first_id),
    )
    conn.execute(
        "UPDATE agents SET current_state='check', ready_at=?, phase='waiting' WHERE id=?",
        (format_utc(utc_now() + timedelta(minutes=48)), second_id),
    )
    conn.execute(
        "UPDATE agents SET current_state='check', ready_at=?, substate='interaction', phase='paused' WHERE id=?",
        (format_utc(utc_now() + timedelta(minutes=32)), third_id),
    )
    conn.commit()

    text = render_list(conn, [dict(row) for row in conn.execute("SELECT * FROM agents ORDER BY id")])
    plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
    rows = [line for line in plain.splitlines() if line.strip().startswith("#")]
    assert len(rows) == 3

    id_positions = [row.index("#") for row in rows]
    status_positions = [re.search(r"\b(waiting|paused)\b", row).start() for row in rows]
    time_positions = [re.search(r"\b\d{2}:\d{2}:\d{2}\b", row).start() for row in rows]

    assert len(set(id_positions)) == 1
    assert len(set(status_positions)) == 1
    assert len(set(time_positions)) == 1


def test_render_list_shows_daemon_crash_block(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    conn = connect()
    init_db(conn)
    set_meta(conn, "daemon_last_exit_kind", "error")
    set_meta(conn, "daemon_last_exit_at", "2026-04-01T15:57:51Z")
    set_meta(conn, "daemon_last_error_at", "2026-04-01T15:57:51Z")
    set_meta(
        conn,
        "daemon_last_error",
        "Traceback (most recent call last):\nRuntimeError: Codex did not become ready in tmux session 'flow-00d1a67e-agent-1'",
    )
    conn.commit()

    text = render_list(conn, [])

    assert "Diagnostics" in text
    assert "daemon exited with error" in text
    assert re.search(r"\d{2}:\d{2} on Apr 1", text)
    assert "Codex did not become ready" in text


def test_render_list_shows_new_diagnostics_since_last_list(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    conn = connect()
    init_db(conn)
    path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: .

start:
  start: true
  prompt: hi
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    flow = render_flow(load_flow(path), {}, cwd_override=str(tmp_path))
    snapshot_id = record_flow_snapshot(conn, flow, str(flow_to_dict(flow)))
    agent_id = create_agent(
        conn,
        flow_snapshot_id=snapshot_id,
        flow_name=flow.name,
        source_path=flow.source_path,
        backend="fake",
        start_state="start",
        cwd=str(tmp_path),
        mode="yolo",
        thinking="xhigh",
        args_json="{}",
    )
    set_meta(conn, "daemon_pid", str(__import__("os").getpid()))
    set_meta(conn, "daemon_started_at", "2026-04-01T15:57:51Z")
    set_meta(conn, "daemon_heartbeat_at", "2026-04-01T15:58:00Z")
    set_meta(conn, "list_last_seen_error_at", "2026-04-01T15:57:00Z")
    record_daemon_event(
        conn,
        level="warning",
        message="agent #1 demo:start backend probe was slow",
        created_at="2026-04-01T15:57:30Z",
        details_text="warning details",
    )
    record_agent_event(
        conn,
        agent_id,
        "error",
        created_at="2026-04-01T15:57:40Z",
        state_name="start",
        reason="command failed",
    )
    conn.commit()

    text = render_list(conn, [dict(row) for row in conn.execute("SELECT * FROM agents")])

    assert "Diagnostics" in text
    assert "new since last list:" in text
    assert re.search(r"\d{2}:\d{2} on Apr 1", text)
    assert "backend probe was slow" in text
    assert "agent #1" in text
    assert "command failed" in text


def test_render_show_formats_header_and_event_log(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    base = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    now_value = base + timedelta(minutes=15)
    monkeypatch.setattr("flow.store.utc_now", lambda: now_value)
    monkeypatch.setattr("flow.render.utc_now", lambda: now_value)

    conn = connect()
    init_db(conn)
    path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: .

start:
  start: true
  prompt: hi
  transitions:
    - go: hold

hold:
  prompt: wait
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    flow = render_flow(load_flow(path), {}, cwd_override=str(tmp_path))
    snapshot_id = record_flow_snapshot(conn, flow, str(flow_to_dict(flow)))
    agent_id = create_agent(
        conn,
        flow_snapshot_id=snapshot_id,
        flow_name=flow.name,
        source_path=flow.source_path,
        backend="fake",
        start_state="start",
        cwd=str(tmp_path),
        mode="yolo",
        thinking="xhigh",
        args_json='{"repo":"tenstorrent/tt-metal","run_url":"https://github.com/example/run"}',
    )
    conn.execute(
        "UPDATE agents SET created_at=?, updated_at=?, state_entered_at=?, current_state=?, status_message=?, phase=?, ended_at=? WHERE id=?",
        (
            format_utc(base),
            format_utc(now_value),
            format_utc(base + timedelta(minutes=5)),
            "hold",
            "Waiting until later",
            "waiting",
            format_utc(now_value),
            agent_id,
        ),
    )
    conn.execute(
        "UPDATE state_runs SET started_at=?, ended_at=? WHERE agent_id=?",
        (format_utc(base), format_utc(base + timedelta(minutes=5)), agent_id),
    )
    record_agent_event(
        conn,
        agent_id,
        "decision",
        created_at=format_utc(base + timedelta(minutes=5)),
        from_state="start",
        to_state="hold",
        choice="hold",
        reason="retry later",
    )
    record_agent_event(
        conn,
        agent_id,
        "delay",
        created_at=format_utc(base + timedelta(minutes=5)),
        state_name="hold",
        reason="Waiting for 10m",
        payload={"wait": "10m", "ready_at": format_utc(now_value)},
    )
    record_agent_event(
        conn,
        agent_id,
        "pause",
        created_at=format_utc(base + timedelta(minutes=5, seconds=30)),
        state_name="hold",
        reason="Paused by alice",
    )
    record_agent_event(
        conn,
        agent_id,
        "interrupt",
        created_at=format_utc(base + timedelta(minutes=6)),
        state_name="hold",
        reason="Interrupted by user",
    )
    record_agent_event(
        conn,
        agent_id,
        "resume",
        created_at=format_utc(base + timedelta(minutes=7)),
        state_name="hold",
        reason="Resumed by user",
    )
    record_agent_event(
        conn,
        agent_id,
        "wake",
        created_at=format_utc(base + timedelta(minutes=8)),
        state_name="hold",
        reason="Woken by alice",
    )
    conn.commit()

    text = render_show(
        conn,
        dict(get_agent(conn, agent_id)),
        [dict(row) for row in conn.execute("SELECT * FROM agent_events WHERE agent_id=? ORDER BY created_at, id", (agent_id,))],
    )

    assert "demo in" in text
    assert "0h 5m running, 0h 10m waiting" in text
    assert "started" in text
    assert re.search(r"\d{2}:\d{2} on Apr 1", text)
    assert "repo:" in text
    assert "run_url:" in text
    assert "(0h  5m):" in text
    assert "start -> hold" in text
    assert "retry later" in text
    assert re.search(r"hold\s+wait for 10m until \d{2}:\d{2} on Apr 1", text)
    assert re.search(r"hold\s+paused", text)
    assert re.search(r"hold\s+interrupted", text)
    assert re.search(r"hold\s+resumed", text)
    assert re.search(r"hold\s+woke", text)


def test_fit_show_top_pins_header_and_keeps_latest_events() -> None:
    text = "\n".join(
        [
            "header",
            "state",
            "",
            "Events",
            "event 1",
            "event 2",
            "event 3",
        ]
    )
    fitted = fit_show_top(text, 6)
    assert fitted.splitlines() == ["header", "state", "", "Events", "event 2", "event 3"]


def test_render_show_pads_single_digit_days_when_mixed(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    base = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    later = datetime(2026, 4, 12, 12, 5, tzinfo=timezone.utc)
    monkeypatch.setattr("flow.store.utc_now", lambda: later)
    monkeypatch.setattr("flow.render.utc_now", lambda: later)

    conn = connect()
    init_db(conn)
    path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: .

start:
  start: true
  prompt: hi
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    flow = render_flow(load_flow(path), {}, cwd_override=str(tmp_path))
    snapshot_id = record_flow_snapshot(conn, flow, str(flow_to_dict(flow)))
    agent_id = create_agent(
        conn,
        flow_snapshot_id=snapshot_id,
        flow_name=flow.name,
        source_path=flow.source_path,
        backend="fake",
        start_state="start",
        cwd=str(tmp_path),
        mode="yolo",
        thinking="xhigh",
        args_json="{}",
    )
    conn.execute(
        "UPDATE agents SET created_at=?, updated_at=?, state_entered_at=?, ended_at=? WHERE id=?",
        (format_utc(base), format_utc(later), format_utc(base), format_utc(later), agent_id),
    )
    record_agent_event(
        conn,
        agent_id,
        "delay",
        created_at=format_utc(base),
        state_name="start",
        payload={"wait": "10m", "ready_at": format_utc(base + timedelta(minutes=10))},
    )
    record_agent_event(
        conn,
        agent_id,
        "decision",
        created_at=format_utc(later),
        from_state="start",
        to_state="done",
        reason="finished later",
    )
    conn.commit()

    text = render_show(
        conn,
        dict(get_agent(conn, agent_id)),
        [dict(row) for row in conn.execute("SELECT * FROM agent_events WHERE agent_id=? ORDER BY created_at, id", (agent_id,))],
    )

    assert re.search(r"\d{2}:\d{2} on Apr  1", text)
    assert re.search(r"\d{2}:\d{2} on Apr 12", text)


def test_render_show_colors_needs_help_substate_and_event_token(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    base = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("flow.store.utc_now", lambda: base)
    monkeypatch.setattr("flow.render.utc_now", lambda: base)

    conn = connect()
    init_db(conn)
    path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: .

start:
  start: true
  prompt: hi
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    flow = render_flow(load_flow(path), {}, cwd_override=str(tmp_path))
    snapshot_id = record_flow_snapshot(conn, flow, str(flow_to_dict(flow)))
    agent_id = create_agent(
        conn,
        flow_snapshot_id=snapshot_id,
        flow_name=flow.name,
        source_path=flow.source_path,
        backend="fake",
        start_state="start",
        cwd=str(tmp_path),
        mode="yolo",
        thinking="xhigh",
        args_json="{}",
    )
    conn.execute(
        "UPDATE agents SET created_at=?, updated_at=?, substate=?, status_message=? WHERE id=?",
        (format_utc(base), format_utc(base), "needs_help", "blocked on logs", agent_id),
    )
    record_agent_event(
        conn,
        agent_id,
        "needs_help",
        created_at=format_utc(base),
        state_name="start",
        reason="blocked on logs",
    )
    conn.commit()

    text = render_show(
        conn,
        dict(get_agent(conn, agent_id)),
        [dict(row) for row in conn.execute("SELECT * FROM agent_events WHERE agent_id=? ORDER BY created_at, id", (agent_id,))],
    )

    red_token = f"\x1b[1;38;5;{PALETTE.error}mneeds_help\x1b[0m"
    assert "Substate" in text
    assert red_token in text
    assert f"{red_token} " in text


def test_create_agent_uses_runtime_specific_tmux_session(tmp_path: Path) -> None:
    conn = connect(tmp_path / "runtime.sqlite3")
    init_db(conn)
    path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: .

start:
  start: true
  prompt: hi
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    flow = render_flow(load_flow(path), {}, cwd_override=str(tmp_path))
    snapshot_id = record_flow_snapshot(conn, flow, str(flow_to_dict(flow)))
    agent_id = create_agent(
        conn,
        flow_snapshot_id=snapshot_id,
        flow_name=flow.name,
        source_path=flow.source_path,
        backend="fake",
        start_state="start",
        cwd=str(tmp_path),
        mode="yolo",
        thinking="xhigh",
        args_json="{}",
    )

    agent = get_agent(conn, agent_id)

    assert agent is not None
    assert str(agent["tmux_session"]).startswith("flow-")
    assert str(agent["tmux_session"]).endswith(f"-agent-{agent_id}")
