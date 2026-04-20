from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from flow.ansi import PALETTE
from flow import __version__
from flow.cli import cmd_catalog, cmd_list, cmd_restart, cmd_show, cmd_top, cmd_validate, cmd_view, main, run_top_mode
from flow.common import format_utc, utc_now
from flow.render import fit_list_top, fit_show_top, fit_top_dashboard, render_list, render_show, render_top_dashboard
from flow.store import (
    connect,
    create_agent,
    get_agent,
    get_meta,
    init_db,
    list_top_agent_events,
    list_top_agents,
    record_agent_event,
    record_daemon_event,
    record_flow_snapshot,
    set_meta,
    update_agent,
)
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


def test_cmd_catalog_outputs_yaml_and_broken_entries(tmp_path: Path, monkeypatch: object, capsys: object) -> None:
    conn = connect(tmp_path / "runtime.sqlite3")
    init_db(conn)
    flows_dir = tmp_path / "flows"
    flows_dir.mkdir()
    write_flow(
        flows_dir / "watch-pr.yaml",
        """
flow:
  name: watch-pr
  description: Watch CI for a PR.
  args:
    pr:
      help: Link to the PR to watch

watch:
  start: true
  prompt: watch
  transitions:
    - go: success

success:
  end: true
""".strip(),
    )
    write_flow(
        flows_dir / "broken.yaml",
        """
flow:
  name: broken

watch:
  start: true
  prompt: watch
  transitions:
    - go: missing
""".strip(),
    )
    monkeypatch.setenv("FLOW_PATH", str(flows_dir))

    assert cmd_catalog(conn, output_format="yaml", include_broken=True) == 0

    payload = yaml.safe_load(capsys.readouterr().out)
    assert payload["flows"][0]["name"] == "watch-pr"
    assert payload["flows"][0]["args"] == {"pr": "Link to the PR to watch"}
    assert payload["broken"][0]["path"].endswith("broken.yaml")


def test_cmd_show_json_reports_waiting_children(tmp_path: Path, capsys: object) -> None:
    conn = connect(tmp_path / "runtime.sqlite3")
    init_db(conn)
    child_path = write_flow(
        tmp_path / "child.yaml",
        """
flow:
  name: child
  path: .

watch:
  start: true
  prompt: watch
  transitions:
    - go: success

success:
  end: true
""".strip(),
    )
    parent_path = write_flow(
        tmp_path / "parent.yaml",
        """
flow:
  name: parent
  path: .

watch-ci:
  start: true
  prompt: wait
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    child_flow = render_flow(load_flow(child_path), {}, cwd_override=str(tmp_path))
    child_snapshot = record_flow_snapshot(conn, child_flow, str(flow_to_dict(child_flow)))
    child_id = create_agent(
        conn,
        flow_snapshot_id=child_snapshot,
        flow_name=child_flow.name,
        source_path=child_flow.source_path,
        backend="fake",
        start_state="watch",
        cwd=str(tmp_path),
        mode="yolo",
        thinking="xhigh",
        args_json='{"pr":"https://example.com/pr/1"}',
    )
    parent_flow = render_flow(load_flow(parent_path), {}, cwd_override=str(tmp_path))
    parent_snapshot = record_flow_snapshot(conn, parent_flow, str(flow_to_dict(parent_flow)))
    parent_id = create_agent(
        conn,
        flow_snapshot_id=parent_snapshot,
        flow_name=parent_flow.name,
        source_path=parent_flow.source_path,
        backend="fake",
        start_state="watch-ci",
        cwd=str(tmp_path),
        mode="workspace-write",
        thinking="xhigh",
        args_json='{"pr":"https://example.com/pr/1"}',
    )
    record_agent_event(
        conn,
        parent_id,
        "wait_children",
        state_name="watch-ci",
        reason="watch CI",
        payload={"child_ids": [child_id, 999], "pending": [child_id], "started_at": format_utc(utc_now())},
    )
    update_agent(
        conn,
        parent_id,
        phase="waiting_children",
        pending_state_json=json.dumps({"kind": "waiting_children", "child_ids": [child_id, 999], "started_at": format_utc(utc_now())}),
        status_message=f"Waiting on child agents #{child_id}",
    )
    conn.execute(
        "UPDATE agents SET current_state='success', phase='finished', ended_at=?, updated_at=? WHERE id=?",
        (format_utc(utc_now()), format_utc(utc_now()), child_id),
    )
    conn.commit()

    assert cmd_show(conn, parent_id, json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["id"] == parent_id
    assert payload["phase"] == "waiting_children"
    assert payload["waiting_on"]["pending"] == []
    finished_entries = {item["id"]: item for item in payload["waiting_on"]["finished"]}
    assert finished_entries[child_id]["end_state"] == "success"
    assert finished_entries[child_id]["status"] == "finished"
    assert finished_entries[999]["end_state"] is None
    assert finished_entries[999]["status"] == "unknown"
    assert payload["latest_event"]["kind"] == "wait_children"


def _create_demo_agent(conn: sqlite3.Connection, tmp_path: Path) -> int:
    path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  path: .

start:
  start: true
  prompt: hi
  transitions:
    - go: success

success:
  end: true
""".strip(),
    )
    flow = render_flow(load_flow(path), {}, cwd_override=str(tmp_path))
    snapshot = record_flow_snapshot(conn, flow, str(flow_to_dict(flow)))
    return create_agent(
        conn,
        flow_snapshot_id=snapshot,
        flow_name=flow.name,
        source_path=flow.source_path,
        backend="fake",
        start_state="start",
        cwd=str(tmp_path),
        mode="yolo",
        thinking="xhigh",
        args_json="{}",
    )


def test_cmd_show_json_finished_agent_reports_finished_contract(tmp_path: Path, capsys: object) -> None:
    conn = connect(tmp_path / "runtime.sqlite3")
    init_db(conn)
    agent_id = _create_demo_agent(conn, tmp_path)
    conn.execute(
        "UPDATE agents SET current_state='success', phase='finished', ended_at=?, updated_at=? WHERE id=?",
        (format_utc(utc_now()), format_utc(utc_now()), agent_id),
    )
    conn.commit()

    assert cmd_show(conn, agent_id, json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["phase"] == "finished"
    assert payload["end_state"] == "success"
    assert payload["terminated_reason"] == "finished"


def test_cmd_show_json_stopped_agent_reports_stopped_contract(tmp_path: Path, capsys: object) -> None:
    conn = connect(tmp_path / "runtime.sqlite3")
    init_db(conn)
    agent_id = _create_demo_agent(conn, tmp_path)
    conn.execute(
        "UPDATE agents SET current_state='stopped', phase='finished', ended_at=?, updated_at=? WHERE id=?",
        (format_utc(utc_now()), format_utc(utc_now()), agent_id),
    )
    conn.commit()

    assert cmd_show(conn, agent_id, json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["phase"] == "stopped"
    assert payload["end_state"] is None
    assert payload["terminated_reason"] == "stopped"


def test_cmd_show_json_running_agent_has_no_terminated_reason(tmp_path: Path, capsys: object) -> None:
    conn = connect(tmp_path / "runtime.sqlite3")
    init_db(conn)
    agent_id = _create_demo_agent(conn, tmp_path)

    assert cmd_show(conn, agent_id, json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["phase"] not in {"finished", "stopped"}
    assert payload["end_state"] is None
    assert payload["terminated_reason"] is None


def test_parse_start_arguments_help_uses_path_metavar(tmp_path: Path, capsys: object) -> None:
    path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  description: Start the demo flow for a single repo.
  path: ~/Work/demo
  args:
    repo:
      help: Repository to inspect
      default: deepseek

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
    compact = " ".join(out.split())
    assert "Start the demo flow for a single repo." in out
    assert "--repo REPO" in out
    assert "default: deepseek" in out
    assert "--path PATH" in out
    assert "default: ~/Work/demo" in compact
    assert "__PATH__" not in out


def test_parse_start_arguments_help_renders_path_default_from_arg_defaults(tmp_path: Path, capsys: object) -> None:
    path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: ./repos/{{repo}}
  args:
    repo:
      help: Repository to inspect
      default: deepseek

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
    assert "default: ./repos/deepseek" in " ".join(out.split())


def test_parse_start_arguments_help_hides_none_defaults(tmp_path: Path, capsys: object) -> None:
    path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: ~/Work/demo
  args:
    issue:
      help: GitHub issue describing a failing CI workflow

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
    assert "default: None" not in out


def test_main_version_prints_and_exits(capsys: object) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])

    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"flow {__version__}"


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
            fast INTEGER NOT NULL DEFAULT 0,
            args_json TEXT NOT NULL,
            tmux_session TEXT NOT NULL,
            thread_id TEXT NOT NULL DEFAULT '',
            rollout_path TEXT NOT NULL DEFAULT '',
            launch_marker TEXT NOT NULL DEFAULT '',
            launch_command TEXT NOT NULL DEFAULT '',
            desired_mode TEXT NOT NULL DEFAULT '',
            desired_thinking TEXT NOT NULL DEFAULT '',
            desired_fast INTEGER NOT NULL DEFAULT 0,
            current_turn_id TEXT NOT NULL DEFAULT '',
            current_turn_kind TEXT NOT NULL DEFAULT '',
            current_turn_started_at TEXT NOT NULL DEFAULT '',
            current_request_id TEXT NOT NULL DEFAULT '',
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


def test_cmd_list_succeeds_while_writer_holds_lock(tmp_path: Path, monkeypatch: object, capsys: object) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    writer = connect()
    init_db(writer)
    reader = connect()
    reader.execute("PRAGMA busy_timeout=1")

    try:
        writer.execute("BEGIN IMMEDIATE")
        init_db(reader)
        assert cmd_list(reader, None) == 0
    finally:
        if writer.in_transaction:
            writer.rollback()
        reader.close()
        writer.close()

    assert "Runtime" in capsys.readouterr().out


def test_cmd_top_uses_top_mode_and_filters_recent_agents(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    now_value = datetime(2026, 4, 13, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("flow.cli.utc_now", lambda: now_value)
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
    - go: done

done:
  end: true
""".strip(),
    )
    flow = render_flow(load_flow(path), {}, cwd_override=str(tmp_path))
    snapshot_id = record_flow_snapshot(conn, flow, str(flow_to_dict(flow)))
    active_id = create_agent(
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
    recent_id = create_agent(
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
    stale_id = create_agent(
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
        "UPDATE agents SET ended_at=?, phase='finished' WHERE id=?",
        (format_utc(now_value - timedelta(minutes=30)), recent_id),
    )
    conn.execute(
        "UPDATE agents SET ended_at=?, phase='finished' WHERE id=?",
        (format_utc(now_value - timedelta(hours=2)), stale_id),
    )
    record_agent_event(
        conn,
        recent_id,
        "decision",
        created_at=format_utc(now_value - timedelta(minutes=20)),
        from_state="start",
        to_state="done",
        reason="finished recently",
    )
    conn.commit()

    seen: dict[str, str] = {}

    class FakeTTY:
        def isatty(self) -> bool:
            return True

    def fake_top(render_once: object, *, fitter: object, on_exit: object = None, refresh_seconds: float = 5.0) -> int:
        del refresh_seconds
        seen["before"] = get_meta(conn, "list_last_seen_error_at")
        assert fitter is fit_top_dashboard
        frame = render_once()
        assert f"#{active_id}" in frame
        assert f"#{recent_id}" in frame
        assert f"#{stale_id}" not in frame
        assert "Recent Events" in frame
        assert "finished recently" in frame
        assert on_exit is not None
        on_exit()
        seen["after"] = get_meta(conn, "list_last_seen_error_at")
        return 0

    monkeypatch.setattr("flow.cli.sys.stdin", FakeTTY())
    monkeypatch.setattr("flow.cli.sys.stdout", FakeTTY())
    monkeypatch.setattr("flow.cli.run_top_mode", fake_top)

    assert cmd_top(conn, None, recent="1h") == 0
    assert seen["before"] == ""
    assert seen["after"]


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


def test_render_list_shows_waiting_children_agents_as_waiting(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    base = datetime(2026, 4, 17, 15, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("flow.render.utc_now", lambda: base + timedelta(minutes=3))
    conn = connect()
    init_db(conn)
    path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: .

watch:
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
        start_state="watch",
        cwd=str(tmp_path),
        mode="yolo",
        thinking="xhigh",
        args_json="{}",
    )
    update_agent(
        conn,
        agent_id,
        phase="waiting_children",
        pending_state_json=json.dumps({"kind": "waiting_children", "child_ids": [17], "started_at": format_utc(base)}),
    )
    conn.commit()

    text = render_list(conn, [dict(row) for row in conn.execute("SELECT * FROM agents")])

    assert f"#{agent_id}" in text
    assert "waiting 00:03:00" in text


def test_render_list_puts_end_states_last_and_dims_them(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr("flow.ansi.ansi_enabled", lambda: True)
    conn = connect()
    init_db(conn)
    path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  path: .

zebra:
  start: true
  prompt: hi
  transitions:
    - go: alpha

alpha:
  end: true
""".strip(),
    )
    flow = render_flow(load_flow(path), {}, cwd_override=str(tmp_path))
    snapshot_id = record_flow_snapshot(conn, flow, str(flow_to_dict(flow)))
    active_id = create_agent(
        conn,
        flow_snapshot_id=snapshot_id,
        flow_name=flow.name,
        source_path=flow.source_path,
        backend="fake",
        start_state="zebra",
        cwd=str(tmp_path),
        mode="yolo",
        thinking="xhigh",
        args_json="{}",
    )
    finished_id = create_agent(
        conn,
        flow_snapshot_id=snapshot_id,
        flow_name=flow.name,
        source_path=flow.source_path,
        backend="fake",
        start_state="zebra",
        cwd=str(tmp_path),
        mode="yolo",
        thinking="xhigh",
        args_json="{}",
    )
    conn.execute(
        "UPDATE agents SET current_state='alpha', phase='finished', ended_at=?, state_entered_at=?, updated_at=? WHERE id=?",
        (format_utc(utc_now()), format_utc(utc_now()), format_utc(utc_now()), finished_id),
    )
    conn.commit()

    text = render_list(conn, [dict(row) for row in conn.execute("SELECT * FROM agents ORDER BY id")])
    plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)

    assert plain.index("  zebra") < plain.index("  alpha")
    assert f"38;5;{PALETTE.dim}m" in text
    assert f"#{active_id}" in plain
    assert f"#{finished_id}" in plain


def test_fit_list_top_truncates_with_summary() -> None:
    text = "one\ntwo\nthree\nfour"
    fitted = fit_list_top(text, 3)
    lines = fitted.splitlines()
    assert lines[:2] == ["one", "two"]
    assert "2 more lines" in lines[2]


def test_fit_top_dashboard_preserves_recent_event_tail() -> None:
    text = "\n".join(
        [
            "summary 1",
            "summary 2",
            "summary 3",
            "summary 4",
            "summary 5",
            "",
            "Recent Events",
            "event 1",
            "event 2",
            "event 3",
            "event 4",
            "event 5",
            "event 6",
            "event 7",
        ]
    )
    fitted = fit_top_dashboard(text, 10)

    assert fitted.splitlines() == [
        "summary 1",
        "... 4 more lines",
        "",
        "Recent Events",
        "event 2",
        "event 3",
        "event 4",
        "event 5",
        "event 6",
        "event 7",
    ]


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

    scratchpad_path = tmp_path / ".flow" / "scratchpads" / f"agent-{agent_id}" / "scratchpad.md"
    scratchpad_path.parent.mkdir(parents=True, exist_ok=True)
    scratchpad_path.write_text("## Settled\n- Retry is safe\n\n## Watch\n- CI still running\n", encoding="utf-8")

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
    assert "\nScratchpad\n" in text
    assert "## Settled" in text
    assert "- Retry is safe" in text
    assert "## Watch" in text
    assert "- CI still running" in text
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


def test_cmd_top_non_tty_prints_once(tmp_path: Path, monkeypatch: object, capsys: object) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    now_value = datetime(2026, 4, 13, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("flow.cli.utc_now", lambda: now_value)
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
    record_agent_event(
        conn,
        agent_id,
        "pause",
        created_at=format_utc(now_value),
        state_name="start",
        reason="Paused by alice",
    )
    conn.commit()

    monkeypatch.setattr("flow.cli.run_top_mode", lambda *args, **kwargs: pytest.fail("run_top_mode should not be used"))

    assert cmd_top(conn, None, recent="1h") == 0
    out = capsys.readouterr().out
    assert "Recent Events" in out
    assert f"#{agent_id}" in out
    assert "Paused by alice" in out


def test_render_top_dashboard_uses_aggregated_event_context(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    now_value = datetime(2026, 4, 13, 10, 0, tzinfo=timezone.utc)
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
    record_agent_event(
        conn,
        agent_id,
        "decision",
        created_at=format_utc(now_value),
        from_state="start",
        to_state="done",
        reason="wrapped up",
    )
    conn.commit()

    agents = [dict(row) for row in list_top_agents(conn, ended_after=format_utc(now_value - timedelta(hours=1)))]
    events = [dict(row) for row in list_top_agent_events(conn, ended_after=format_utc(now_value - timedelta(hours=1)), limit=None)]
    text = render_top_dashboard(conn, agents, events)

    assert "Recent Events" in text
    assert f"#{agent_id}" in text
    assert "demo/start" in text
    assert "wrapped up" in text


def test_render_show_includes_codex_thread_and_resume_hint_for_finished_agent(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    base = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    later = datetime(2026, 4, 1, 12, 5, tzinfo=timezone.utc)
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
        "UPDATE agents SET created_at=?, updated_at=?, state_entered_at=?, thread_id=?, ended_at=? WHERE id=?",
        (format_utc(base), format_utc(later), format_utc(base), "thread-123", format_utc(later), agent_id),
    )
    record_agent_event(
        conn,
        agent_id,
        "decision",
        created_at=format_utc(later),
        from_state="start",
        to_state="done",
        reason="finished",
    )
    conn.commit()

    text = render_show(
        conn,
        dict(get_agent(conn, agent_id)),
        [dict(row) for row in conn.execute("SELECT * FROM agent_events WHERE agent_id=? ORDER BY created_at, id", (agent_id,))],
    )

    assert "Scratchpad" in text
    assert str(tmp_path / ".flow" / "scratchpads" / f"agent-{agent_id}" / "scratchpad.md") in text
    assert "No scratchpad content yet." in text
    assert "Codex" in text
    assert "thread-123" in text
    assert f"codex --cd {tmp_path} resume thread-123" in text


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

    red_token = f"\x1b[1;38;5;{PALETTE.error}mneeds-help\x1b[0m"
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
