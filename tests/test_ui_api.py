from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from flow.cli import cmd_ui
from flow.flowfile import flow_to_dict, load_flow, render_flow
from flow.store import (
    close_open_state_run,
    connect,
    create_agent,
    get_agent,
    init_db,
    open_state_run,
    record_agent_event,
    record_daemon_event,
    record_flow_snapshot,
    set_daemon_status,
    update_agent,
)
from flow.ui_data import build_focus_snapshot, build_overview_snapshot
from flow.ui_server import create_ui_app


def write_flow(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def create_runtime_agent(conn: Any, flow_path: Path, values: dict[str, str]) -> int:
    flow = load_flow(flow_path)
    rendered = render_flow(flow, values, cwd_override=str(flow_path.parent))
    snapshot_id = record_flow_snapshot(conn, rendered, json.dumps(flow_to_dict(rendered), sort_keys=True))
    agent_id = create_agent(
        conn,
        flow_snapshot_id=snapshot_id,
        flow_name=rendered.name,
        source_path=rendered.source_path,
        backend="fake",
        start_state=rendered.start_states[0],
        cwd=rendered.path or str(flow_path.parent),
        mode=rendered.mode or "yolo",
        thinking=rendered.thinking or "xhigh",
        args_json=json.dumps(values, sort_keys=True),
    )
    update_agent(conn, agent_id, launch_marker=f"fake-{agent_id}")
    conn.commit()
    return agent_id


def test_build_overview_snapshot_groups_agent_rows(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    conn = connect()
    init_db(conn)
    flow_path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  args:
    site:
      default: news.ycombinator.com

check:
  start: true
  prompt: Inspect {{site}}
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    working_id = create_runtime_agent(conn, flow_path, {"site": "news.ycombinator.com"})
    waiting_id = create_runtime_agent(conn, flow_path, {"site": "reddit.com/r/locallama"})
    paused_id = create_runtime_agent(conn, flow_path, {"site": "https://karpathy.github.io"})

    update_agent(conn, waiting_id, ready_at="2030-01-01T00:10:00Z", phase="waiting")
    update_agent(conn, paused_id, substate="interaction", phase="paused")

    snapshot = build_overview_snapshot(conn, "demo")

    assert snapshot["flow"]["counts"] == {
        "waiting": 1,
        "working": 1,
        "paused": 1,
        "needs_help": 0,
    }
    check_state = next(item for item in snapshot["flow"]["states"] if item["name"] == "check")
    waiting_rows = check_state["rows"]["waiting"]
    working_rows = check_state["rows"]["working"]
    paused_rows = check_state["rows"]["paused"]
    assert waiting_rows[0]["id"] == waiting_id
    assert waiting_rows[0]["display_args"] == {"site": "reddit.com/r/locallama"}
    assert working_rows[0]["id"] == working_id
    assert working_rows[0]["display_args"] == {}
    assert paused_rows[0]["id"] == paused_id
    edge = next(item for item in snapshot["flow"]["edges"] if item["key"] == "check->done")
    assert edge["transition_label_text"] == ""


def test_build_focus_snapshot_aggregates_state_visits_and_edges(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    conn = connect()
    init_db(conn)
    flow_path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo

check:
  start: true
  prompt: Check
  transitions:
    - if: keep polling
      go: check
    - if: failed
      go: investigate

investigate:
  prompt: Investigate
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    agent_id = create_runtime_agent(conn, flow_path, {})
    now = "2026-04-01T20:00:00Z"
    update_agent(conn, agent_id, created_at=now, current_state="investigate")
    conn.execute(
        "UPDATE state_runs SET started_at=? WHERE agent_id=? AND ended_at=''",
        (now, agent_id),
    )
    close_open_state_run(conn, agent_id, ended_at="2026-04-01T20:02:00Z")
    open_state_run(conn, agent_id, "check", started_at="2026-04-01T20:10:00Z")
    close_open_state_run(conn, agent_id, ended_at="2026-04-01T20:14:00Z")
    open_state_run(conn, agent_id, "investigate", started_at="2026-04-01T20:20:00Z")

    record_agent_event(
        conn,
        agent_id,
        "decision",
        created_at="2026-04-01T20:10:00Z",
        from_state="check",
        to_state="check",
        choice="check",
        reason="Still running",
    )
    record_agent_event(
        conn,
        agent_id,
        "decision",
        created_at="2026-04-01T20:20:00Z",
        from_state="check",
        to_state="investigate",
        choice="investigate",
        reason="Found failure",
    )
    conn.commit()

    snapshot = build_focus_snapshot(conn, "demo", agent_id)

    assert snapshot["focus"]["agent"]["id"] == agent_id
    assert snapshot["focus"]["states"]["check"]["count"] == 2
    assert snapshot["focus"]["states"]["check"]["latest_duration_text"] == "0h 4m"
    assert snapshot["focus"]["states"]["check"]["total_duration_text"] == "0h 6m"
    assert snapshot["focus"]["edges"]["check->check"]["count"] == 1
    assert snapshot["focus"]["edges"]["check->investigate"]["latest_reason"] == "Found failure"
    structural_edge = next(item for item in snapshot["flow"]["edges"] if item["key"] == "check->check")
    assert structural_edge["transition_label_text"] == ""
    assert any('check -> investigate "Found failure"' in item["text"] for item in snapshot["focus"]["events"])


def test_build_overview_snapshot_includes_wait_in_edge_labels(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    conn = connect()
    init_db(conn)
    flow_path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo

check:
  start: true
  prompt: Check
  transitions:
    - if: still running
      wait: 10m
      go: check
    - go: done

done:
  end: true
""".strip(),
    )
    create_runtime_agent(conn, flow_path, {})

    snapshot = build_overview_snapshot(conn, "demo")

    edge = next(item for item in snapshot["flow"]["edges"] if item["key"] == "check->check")
    assert edge["transition_label_text"] == "[10m wait]"


def test_ui_api_overview_endpoint_returns_flow_snapshot(tmp_path: Path, monkeypatch: Any) -> None:
    flow_home = tmp_path / ".flow"
    monkeypatch.setenv("FLOW_HOME", str(flow_home))
    conn = connect()
    init_db(conn)
    flow_path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo

check:
  start: true
  prompt: Check
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    create_runtime_agent(conn, flow_path, {})

    client = TestClient(create_ui_app())
    response = client.get("/api/flows/demo")

    assert response.status_code == 200
    payload = response.json()
    assert payload["flow"]["name"] == "demo"
    assert payload["flow"]["states"][0]["name"] == "check"


def test_build_overview_snapshot_filters_stale_runtime_diagnostics(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    conn = connect()
    init_db(conn)
    flow_path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo

check:
  start: true
  prompt: Check
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    create_runtime_agent(conn, flow_path, {})
    set_daemon_status(conn, os.getpid(), started_at="2026-04-02T10:00:00Z", heartbeat_at="2026-04-02T10:01:00Z")
    record_daemon_event(
        conn,
        level="warning",
        message="old warning",
        created_at="2026-04-01T10:00:00Z",
    )
    record_daemon_event(
        conn,
        level="warning",
        message="fresh warning",
        created_at="2026-04-02T10:10:00Z",
    )

    snapshot = build_overview_snapshot(conn, "demo")

    assert [item["message"] for item in snapshot["runtime"]["diagnostics"]] == ["fresh warning"]


def test_cmd_ui_launches_tauri_dev_with_flow_name(tmp_path: Path, monkeypatch: Any) -> None:
    conn = connect(tmp_path / "runtime.sqlite3")
    init_db(conn)
    flow_path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo

check:
  start: true
  prompt: Check
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    create_runtime_agent(conn, flow_path, {})

    ui_dir = Path(__file__).resolve().parents[1] / "ui"
    ui_dir.mkdir(exist_ok=True)
    if not (ui_dir / "package.json").exists():
        (ui_dir / "package.json").write_text("{}", encoding="utf-8")

    launched: dict[str, Any] = {}

    class FakeHandle:
        url = "http://127.0.0.1:4123"

        def close(self) -> None:
            launched["closed"] = True

    monkeypatch.setattr("flow.cli.start_ui_server", lambda: FakeHandle())
    monkeypatch.setattr("flow.cli.shutil.which", lambda _name: "/opt/homebrew/bin/npm")

    def fake_call(command: list[str], cwd: Path, env: dict[str, str]) -> int:
        launched["command"] = command
        launched["cwd"] = str(cwd)
        launched["env_flow"] = env["FLOW_UI_FLOW_NAME"]
        launched["env_url"] = env["FLOW_UI_API_BASE_URL"]
        return 0

    monkeypatch.setattr("flow.cli.subprocess.call", fake_call)

    assert cmd_ui(conn, "demo") == 0
    assert launched["command"] == [
        "npm",
        "run",
        "tauri:dev",
    ]
    assert launched["env_flow"] == "demo"
    assert launched["env_url"] == "http://127.0.0.1:4123"
    assert launched["closed"] is True
