from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from flow.backend import AgentBackend, TurnObservation
from flow.common import format_utc, utc_now
from flow.flowfile import flow_to_dict, load_flow, render_flow
from flow.runtime import Runtime, parse_decision
from flow.store import (
    connect,
    create_agent,
    daemon_exit_info,
    enqueue_command,
    get_agent,
    init_db,
    list_agent_events,
    list_agents,
    record_flow_snapshot,
    set_meta,
    update_agent,
)


class FakeBackend(AgentBackend):
    def __init__(self) -> None:
        self.sessions: dict[int, bool] = {}
        self.scripts: dict[int, list[str]] = {}
        self.prompts: dict[int, list[str]] = {}
        self.request_ids: dict[int, list[str]] = {}
        self.thread_name_calls: list[tuple[int, str]] = []
        self.thread_name_result: bool | None = None
        self.turn_counter = 0

    def set_script(self, agent_id: int, outputs: list[str]) -> None:
        self.scripts[agent_id] = list(outputs)

    def ensure_session(self, agent: dict[str, Any]) -> dict[str, str]:
        agent_id = int(agent["id"])
        self.sessions[agent_id] = True
        return {"launch_command": f"fake-launch-{agent_id}"}

    def send_prompt(self, agent: dict[str, Any], prompt: str, *, request_id: str = "") -> TurnObservation:
        agent_id = int(agent["id"])
        self.prompts.setdefault(agent_id, []).append(prompt)
        prompt_request_id = request_id.strip()
        prompt_kind = ""
        for line in prompt.splitlines():
            stripped = line.strip()
            if stripped.startswith("request_id:") and not prompt_request_id:
                prompt_request_id = stripped.split(":", 1)[1].strip()
            if stripped.startswith("kind:"):
                prompt_kind = stripped.split(":", 1)[1].strip()
        if prompt_request_id and prompt_kind in {"transition_eval", "terminal_eval"}:
            self.request_ids.setdefault(agent_id, []).append(prompt_request_id)
        return TurnObservation(status="running", started_at=format_utc(utc_now()))

    def set_thread_name(self, agent: dict[str, Any], name: str) -> bool | None:
        self.thread_name_calls.append((int(agent["id"]), name))
        return self.thread_name_result

    def interrupt(self, agent: dict[str, Any]) -> None:
        return None

    def terminate(self, agent: dict[str, Any], *, immediate: bool) -> None:
        self.sessions[int(agent["id"])] = False

    def attach(self, agent: dict[str, Any]) -> int:
        return 0

    def attach_many(self, agents: list[dict[str, Any]]) -> int:
        return 0

    def session_exists(self, agent: dict[str, Any]) -> bool:
        return self.sessions.get(int(agent["id"]), False)

    def poll_turn(self, agent: dict[str, Any]) -> TurnObservation:
        agent_id = int(agent["id"])
        outputs = self.scripts.get(agent_id) or []
        if not outputs:
            return TurnObservation(status="pending")
        self.turn_counter += 1
        text = outputs.pop(0)
        pending_request_ids = self.request_ids.get(agent_id) or []
        if pending_request_ids:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and "choice" in payload and "request_id" not in payload:
                payload["request_id"] = pending_request_ids.pop(0)
                text = json.dumps(payload)
        return TurnObservation(
            status="completed",
            thread_id=f"thread-{agent_id}",
            rollout_path=f"/tmp/fake-{agent_id}.jsonl",
            turn_id=f"turn-{self.turn_counter}",
            started_at=agent["current_turn_started_at"],
            ended_at=format_utc(utc_now()),
            output_text=text,
            raw_output=text,
        )


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


def test_runtime_reaches_end_state(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    conn = connect()
    init_db(conn)
    flow_path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: .

check:
  start: true
  prompt: work
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    agent_id = create_runtime_agent(conn, flow_path, {})
    backend = FakeBackend()
    backend.set_script(agent_id, ["worked", '{"choice":"done","reason":"finished"}'])
    runtime = Runtime(backend=backend)

    for _ in range(4):
        runtime.tick(conn)

    agent = dict(get_agent(conn, agent_id))
    assert agent["current_state"] == "done"
    assert agent["ended_at"]
    assert f"[flow {agent_id}] demo" in backend.prompts[agent_id][0]
    assert backend.prompts[agent_id][0].count("State: check")
    assert f"Scratchpad file: {tmp_path / '.flow' / 'scratchpads' / f'agent-{agent_id}' / 'scratchpad.md'}" in backend.prompts[agent_id][0]
    assert "Flow runtime:" in backend.prompts[agent_id][0]
    assert "Optional headings:" in backend.prompts[agent_id][0]
    assert f"[flow {agent_id}] demo" not in backend.prompts[agent_id][1]
    assert f"Scratchpad file: {tmp_path / '.flow' / 'scratchpads' / f'agent-{agent_id}' / 'scratchpad.md'}" in backend.prompts[agent_id][1]
    assert "Flow runtime:" not in backend.prompts[agent_id][1]
    assert "Optional headings:" not in backend.prompts[agent_id][1]


def test_runtime_first_prompt_includes_non_default_args_in_thread_name(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    conn = connect()
    init_db(conn)
    flow_path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: deploy
  version: 1
  path: .
  args:
    env:
      default: staging
    service: {}

check:
  start: true
  prompt: verify rollout
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    agent_id = create_runtime_agent(conn, flow_path, {"env": "prod", "service": "payments"})
    backend = FakeBackend()
    backend.set_script(agent_id, ["worked"])
    runtime = Runtime(backend=backend)

    runtime.tick(conn)

    assert (
        f"[flow {agent_id}] deploy env=prod service=payments" in backend.prompts[agent_id][0]
    )


def test_parse_decision_normalizes_legacy_implicit_transition_aliases() -> None:
    assert parse_decision('{"choice":"needs_help","reason":"blocked"}').choice == "needs-help"
    assert parse_decision('{"choice":"keep_working","reason":"one more thing"}').choice == "keep-working"


def test_parse_decision_requires_matching_request_id_when_expected() -> None:
    decision = parse_decision(
        '{"request_id":"req-123","choice":"keep-working","reason":"one more thing"}',
        expected_request_id="req-123",
    )

    assert decision.request_id == "req-123"
    assert decision.choice == "keep-working"

    with pytest.raises(ValueError, match="missing 'request_id'"):
        parse_decision('{"choice":"keep-working","reason":"one more thing"}', expected_request_id="req-123")

    with pytest.raises(ValueError, match="request_id mismatch"):
        parse_decision(
            '{"request_id":"req-999","choice":"keep-working","reason":"one more thing"}',
            expected_request_id="req-123",
        )


def test_parse_decision_accepts_wait_for_child_with_child_ids() -> None:
    decision = parse_decision(
        '{"choice":"wait-for-child","child_ids":[17,"18",17],"reason":"watch CI"}'
    )

    assert decision.choice == "wait-for-child"
    assert decision.child_ids == (17, 18)
    assert decision.reason == "watch CI"


def test_parse_decision_normalizes_legacy_wait_for_child_spelling() -> None:
    decision = parse_decision(
        '{"choice":"wait_for_child","child_ids":[17],"reason":"watch CI"}'
    )

    assert decision.choice == "wait-for-child"
    assert decision.child_ids == (17,)


def test_parse_decision_rejects_action_key() -> None:
    with pytest.raises(ValueError, match="missing 'choice'"):
        parse_decision(
            '{"action":"wait-for-child","child_ids":[17],"reason":"watch CI"}'
        )


def test_parse_decision_requires_child_ids_for_wait_for_child() -> None:
    with pytest.raises(ValueError, match="child_ids"):
        parse_decision('{"choice":"wait-for-child","reason":"watch CI"}')

    with pytest.raises(ValueError, match="non-empty"):
        parse_decision(
            '{"choice":"wait-for-child","child_ids":[],"reason":"watch CI"}'
        )


def test_runtime_sets_thread_name_once_after_first_completed_turn(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    conn = connect()
    init_db(conn)
    flow_path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: .

check:
  start: true
  prompt: work
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    agent_id = create_runtime_agent(conn, flow_path, {})
    backend = FakeBackend()
    backend.thread_name_result = True
    backend.set_script(agent_id, ["worked", '{"choice":"done","reason":"finished"}'])
    runtime = Runtime(backend=backend)

    for _ in range(4):
        runtime.tick(conn)

    assert backend.thread_name_calls == [(agent_id, f"[flow {agent_id}] demo")]
    events = [dict(row) for row in list_agent_events(conn, agent_id)]
    assert [event["kind"] for event in events if event["kind"].startswith("thread_name")] == ["thread_name_set"]


def test_runtime_runs_prompted_end_state_before_finishing(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    conn = connect()
    init_db(conn)
    flow_path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: .

check:
  start: true
  prompt: work
  transitions:
    - go: done

done:
  prompt: wrap up
  end: true
""".strip(),
    )
    agent_id = create_runtime_agent(conn, flow_path, {})
    backend = FakeBackend()
    backend.set_script(
        agent_id,
        [
            "worked",
            '{"choice":"done","reason":"finished"}',
            "wrapped up",
            '{"choice":"finish","reason":"all done"}',
        ],
    )
    runtime = Runtime(backend=backend)
    for _ in range(8):
        runtime.tick(conn)
    agent = dict(get_agent(conn, agent_id))
    assert agent["current_state"] == "done"
    assert agent["ended_at"]
    assert len(backend.prompts[agent_id]) == 4
    assert "State: done" in backend.prompts[agent_id][2]
    assert "kind: terminal_eval" in backend.prompts[agent_id][3]
    assert 'Respond with JSON only in the form {"request_id": "' in backend.prompts[agent_id][1]
    assert 'Respond with JSON only in the form {"request_id": "' in backend.prompts[agent_id][3]


def test_runtime_handles_keep_working_then_finishes_terminal_state(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    conn = connect()
    init_db(conn)
    flow_path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: .

check:
  start: true
  prompt: work
  transitions:
    - go: done

done:
  prompt: wrap up
  end: true
""".strip(),
    )
    agent_id = create_runtime_agent(conn, flow_path, {})
    backend = FakeBackend()
    backend.set_script(
        agent_id,
        [
            "worked once",
            '{"choice":"done","reason":"ready for wrap-up"}',
            "wrapped once",
            '{"choice":"keep-working","reason":"one more thing"}',
            "wrapped twice",
            '{"choice":"finish","reason":"finished"}',
        ],
    )
    runtime = Runtime(backend=backend)
    for _ in range(12):
        runtime.tick(conn)
    agent = dict(get_agent(conn, agent_id))
    assert agent["current_state"] == "done"
    assert agent["ended_at"]
    assert len(backend.prompts[agent_id]) == 6


def test_runtime_enters_needs_help_and_resume(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    conn = connect()
    init_db(conn)
    flow_path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: .

check:
  start: true
  prompt: work
  transitions:
    - go: done

done:
  prompt: wrap up
  end: true
""".strip(),
    )
    agent_id = create_runtime_agent(conn, flow_path, {})
    backend = FakeBackend()
    backend.set_script(
        agent_id,
        [
            "worked",
            '{"choice":"done","reason":"ready for wrap-up"}',
            "wrapped",
            '{"choice":"needs-help","reason":"blocked"}',
            "resumed wrap-up",
            '{"choice":"finish","reason":"finished"}',
        ],
    )
    runtime = Runtime(backend=backend)
    for _ in range(8):
        runtime.tick(conn)
    agent = dict(get_agent(conn, agent_id))
    assert agent["substate"] == "needs_help"

    enqueue_command(conn, agent_id, "resume", {})
    conn.commit()
    for _ in range(6):
        runtime.tick(conn)
    agent = dict(get_agent(conn, agent_id))
    assert agent["current_state"] == "done"
    assert agent["ended_at"]


def test_runtime_promptless_end_state_waits_then_finishes_without_prompt(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    conn = connect()
    init_db(conn)
    flow_path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: .

check:
  start: true
  prompt: work
  transitions:
    - go: done

done:
  wait: 10m
  end: true
""".strip(),
    )
    base = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    now_box = {"value": base}
    monkeypatch.setattr("flow.runtime.utc_now", lambda: now_box["value"])

    agent_id = create_runtime_agent(conn, flow_path, {})
    backend = FakeBackend()
    backend.set_script(agent_id, ["worked", '{"choice":"done","reason":"finished"}'])
    runtime = Runtime(backend=backend)

    for _ in range(4):
        runtime.tick(conn)
    agent = dict(get_agent(conn, agent_id))
    assert agent["current_state"] == "done"
    assert agent["phase"] == "waiting"
    assert agent["ready_at"] == format_utc(base + timedelta(minutes=10))

    now_box["value"] = base + timedelta(minutes=10, seconds=1)
    runtime.tick(conn)
    agent = dict(get_agent(conn, agent_id))
    assert agent["current_state"] == "done"
    assert agent["ended_at"]
    assert len(backend.prompts[agent_id]) == 2


def test_runtime_interrupt_move_and_stop(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    monkeypatch.setattr("flow.runtime.current_actor", lambda: "alice")
    conn = connect()
    init_db(conn)
    flow_path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: .

first:
  start: true
  prompt: one
  transitions:
    - go: second

second:
  prompt: two
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    agent_id = create_runtime_agent(conn, flow_path, {})
    backend = FakeBackend()
    runtime = Runtime(backend=backend)

    enqueue_command(conn, agent_id, "interrupt", {})
    conn.commit()
    runtime.tick(conn)
    agent = dict(get_agent(conn, agent_id))
    assert agent["substate"] == "interaction"

    enqueue_command(conn, agent_id, "move", {"state": "second"})
    conn.commit()
    runtime.tick(conn)
    agent = dict(get_agent(conn, agent_id))
    assert agent["current_state"] == "second"

    enqueue_command(conn, agent_id, "stop", {})
    conn.commit()
    runtime.tick(conn)
    agent = dict(get_agent(conn, agent_id))
    assert agent["current_state"] == "stopped"
    assert agent["ended_at"]

    events = [dict(row) for row in list_agent_events(conn, agent_id)]
    assert [event["kind"] for event in events] == ["started", "interrupt", "decision", "decision"]
    assert events[2]["from_state"] == "first"
    assert events[2]["to_state"] == "second"
    assert events[3]["choice"] == "stop"
    assert events[1]["reason"] == "Interrupted by alice"
    assert events[2]["reason"] == "Moved to second by alice"
    assert events[3]["reason"] == "Stopped by alice"


def test_runtime_pause_does_not_interrupt_running_turn(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    monkeypatch.setattr("flow.runtime.current_actor", lambda: "alice")
    conn = connect()
    init_db(conn)
    flow_path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: .

first:
  start: true
  prompt: one
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    agent_id = create_runtime_agent(conn, flow_path, {})

    class SlowBackend(FakeBackend):
        def __init__(self) -> None:
            super().__init__()
            self.poll_counts: dict[int, int] = {}
            self.interrupts: list[int] = []

        def interrupt(self, agent: dict[str, Any]) -> None:
            self.interrupts.append(int(agent["id"]))

        def poll_turn(self, agent: dict[str, Any]) -> TurnObservation:
            agent_id = int(agent["id"])
            count = self.poll_counts.get(agent_id, 0)
            self.poll_counts[agent_id] = count + 1
            if count == 0:
                return TurnObservation(status="running", thread_id=f"thread-{agent_id}", rollout_path=f"/tmp/fake-{agent_id}.jsonl")
            return TurnObservation(
                status="completed",
                thread_id=f"thread-{agent_id}",
                rollout_path=f"/tmp/fake-{agent_id}.jsonl",
                turn_id="turn-1",
                started_at=agent["current_turn_started_at"],
                ended_at=format_utc(utc_now()),
                output_text="worked",
                raw_output="worked",
            )

    backend = SlowBackend()
    runtime = Runtime(backend=backend)

    runtime.tick(conn)
    agent = dict(get_agent(conn, agent_id))
    assert agent["phase"] == "working"
    assert agent["current_turn_started_at"]

    enqueue_command(conn, agent_id, "pause", {})
    conn.commit()
    runtime.tick(conn)

    agent = dict(get_agent(conn, agent_id))
    assert agent["substate"] == "interaction"
    assert agent["phase"] == "paused"
    assert agent["current_turn_started_at"]
    assert backend.interrupts == []

    runtime.tick(conn)
    agent = dict(get_agent(conn, agent_id))
    assert agent["substate"] == "interaction"
    assert agent["phase"] == "paused"
    assert agent["current_turn_started_at"] == ""
    assert len(backend.prompts[agent_id]) == 1

    events = [dict(row) for row in list_agent_events(conn, agent_id)]
    assert [event["kind"] for event in events] == ["started", "pause"]
    assert events[1]["reason"] == "Paused by alice"


def test_graceful_shutdown_suspends_agents(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    conn = connect()
    init_db(conn)
    flow_path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: .

first:
  start: true
  prompt: one
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    agent_id = create_runtime_agent(conn, flow_path, {})
    backend = FakeBackend()
    backend.set_script(agent_id, ["finished current turn"])
    runtime = Runtime(backend=backend)
    runtime.tick(conn)
    set_meta(conn, "shutdown_mode", "graceful")
    set_meta(conn, "shutdown_flow", "")
    conn.commit()
    runtime.tick(conn)
    runtime.tick(conn)
    agent = dict(get_agent(conn, agent_id))
    assert agent["phase"] == "suspended"


def test_runtime_wait_state_delays_then_auto_advances(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    conn = connect()
    init_db(conn)
    flow_path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: .

cooldown:
  start: true
  wait: 10m
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    base = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    now_box = {"value": base}
    monkeypatch.setattr("flow.runtime.utc_now", lambda: now_box["value"])

    agent_id = create_runtime_agent(conn, flow_path, {})
    backend = FakeBackend()
    runtime = Runtime(backend=backend)

    runtime.tick(conn)
    agent = dict(get_agent(conn, agent_id))
    assert agent["phase"] == "waiting"
    assert agent["ready_at"] == format_utc(base + timedelta(minutes=10))
    assert agent_id not in backend.prompts
    events = [dict(row) for row in list_agent_events(conn, agent_id)]
    assert events[-1]["kind"] == "delay"
    assert "10m" in events[-1]["payload_json"]


def test_runtime_wake_clears_delay_and_starts_work(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    monkeypatch.setattr("flow.runtime.current_actor", lambda: "alice")
    conn = connect()
    init_db(conn)
    flow_path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: .

check:
  start: true
  wait: 10m
  prompt: work
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    base = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    now_box = {"value": base}
    monkeypatch.setattr("flow.runtime.utc_now", lambda: now_box["value"])

    agent_id = create_runtime_agent(conn, flow_path, {})
    backend = FakeBackend()
    runtime = Runtime(backend=backend)

    runtime.tick(conn)
    agent = dict(get_agent(conn, agent_id))
    assert agent["phase"] == "waiting"
    assert agent["ready_at"] == format_utc(base + timedelta(minutes=10))

    enqueue_command(conn, agent_id, "wake", {})
    conn.commit()
    runtime.tick(conn)

    agent = dict(get_agent(conn, agent_id))
    assert agent["ready_at"] == ""
    assert agent["phase"] == "working"
    assert backend.prompts[agent_id]

    events = [dict(row) for row in list_agent_events(conn, agent_id)]
    assert [event["kind"] for event in events][-2:] == ["delay", "wake"]
    assert events[-1]["reason"] == "Woken by alice"


def test_runtime_wake_does_not_resume_paused_agent(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    monkeypatch.setattr("flow.runtime.current_actor", lambda: "alice")
    conn = connect()
    init_db(conn)
    flow_path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: .

check:
  start: true
  wait: 10m
  prompt: work
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    base = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    now_box = {"value": base}
    monkeypatch.setattr("flow.runtime.utc_now", lambda: now_box["value"])

    agent_id = create_runtime_agent(conn, flow_path, {})
    backend = FakeBackend()
    runtime = Runtime(backend=backend)

    runtime.tick(conn)
    enqueue_command(conn, agent_id, "interrupt", {})
    conn.commit()
    runtime.tick(conn)

    enqueue_command(conn, agent_id, "wake", {})
    conn.commit()
    runtime.tick(conn)

    agent = dict(get_agent(conn, agent_id))
    assert agent["substate"] == "interaction"
    assert agent["phase"] == "paused"
    assert agent["ready_at"] == ""
    assert agent_id not in backend.prompts

    now_box["value"] = base + timedelta(minutes=10, seconds=1)
    runtime.tick(conn)
    agent = dict(get_agent(conn, agent_id))
    assert agent["current_state"] == "check"
    assert agent["substate"] == "interaction"
    assert agent["phase"] == "paused"

    enqueue_command(conn, agent_id, "resume", {})
    conn.commit()
    runtime.tick(conn)
    agent = dict(get_agent(conn, agent_id))
    assert agent["phase"] == "working"
    assert backend.prompts[agent_id]


def test_runtime_wake_ignores_non_waiting_agent(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    monkeypatch.setattr("flow.runtime.current_actor", lambda: "alice")
    conn = connect()
    init_db(conn)
    flow_path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: .

check:
  start: true
  prompt: work
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )

    agent_id = create_runtime_agent(conn, flow_path, {})
    backend = FakeBackend()
    runtime = Runtime(backend=backend)

    enqueue_command(conn, agent_id, "wake", {})
    conn.commit()
    runtime.tick(conn)

    agent = dict(get_agent(conn, agent_id))
    assert agent["last_error"] == ""
    assert agent["status_message"] == "Waiting for state_prompt"
    events = [dict(row) for row in list_agent_events(conn, agent_id)]
    assert [event["kind"] for event in events] == ["started"]


def test_runtime_pauses_after_aborted_turn_from_resume_state(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    conn = connect()
    init_db(conn)
    flow_path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: .

check:
  start: true
  prompt: work
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    agent_id = create_runtime_agent(conn, flow_path, {})

    class AbortBackend(FakeBackend):
        def __init__(self) -> None:
            super().__init__()
            self.poll_count = 0

        def poll_turn(self, agent: dict[str, Any]) -> TurnObservation:
            self.poll_count += 1
            if self.poll_count == 1:
                return TurnObservation(
                    status="aborted",
                    thread_id=f"thread-{agent['id']}",
                    rollout_path=f"/tmp/fake-{agent['id']}.jsonl",
                    turn_id="turn-1",
                    started_at=agent["current_turn_started_at"],
                    ended_at=format_utc(utc_now()),
                    abort_reason="interrupted",
                )
            return super().poll_turn(agent)

    backend = AbortBackend()
    runtime = Runtime(backend=backend)

    runtime.tick(conn)
    runtime.tick(conn)
    agent = dict(get_agent(conn, agent_id))
    assert agent["phase"] == "paused"
    assert agent["substate"] == "interaction"
    assert agent["current_turn_started_at"] == ""

    events = [dict(row) for row in list_agent_events(conn, agent_id)]
    assert [event["kind"] for event in events][-1] == "error"
    assert json.loads(events[-1]["payload_json"])["auto_retry"] is False
    assert "interrupted" in events[-1]["reason"]


def test_runtime_wait_for_child_parks_and_wakes_in_same_state(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    conn = connect()
    init_db(conn)
    child_flow_path = write_flow(
        tmp_path / "child.yaml",
        """
flow:
  name: child
  version: 1
  path: .

watch:
  start: true
  prompt: monitor the PR
  transitions:
    - go: success

success:
  end: true
""".strip(),
    )
    parent_flow_path = write_flow(
        tmp_path / "parent.yaml",
        """
flow:
  name: parent
  version: 1
  path: .

watch-ci:
  start: true
  prompt: wait for the child flow to finish
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    child_id = create_runtime_agent(conn, child_flow_path, {})
    parent_id = create_runtime_agent(conn, parent_flow_path, {})

    class SlowChildBackend(FakeBackend):
        def __init__(self) -> None:
            super().__init__()
            self.poll_counts: dict[int, int] = {}

        def poll_turn(self, agent: dict[str, Any]) -> TurnObservation:
            agent_id = int(agent["id"])
            self.poll_counts[agent_id] = self.poll_counts.get(agent_id, 0) + 1
            if agent_id == child_id and self.poll_counts[agent_id] == 1:
                return TurnObservation(status="running", thread_id=f"thread-{agent_id}", rollout_path=f"/tmp/fake-{agent_id}.jsonl")
            return super().poll_turn(agent)

    backend = SlowChildBackend()
    backend.set_script(child_id, ["child worked", '{"choice":"success","reason":"green"}'])
    backend.set_script(
        parent_id,
        [
            "started child watch",
            f'{{"choice":"wait-for-child","child_ids":[{child_id}],"reason":"wait for CI"}}',
            "child finished cleanly",
            '{"choice":"done","reason":"all set"}',
        ],
    )
    runtime = Runtime(backend=backend)

    runtime.tick(conn)
    runtime.tick(conn)
    runtime.tick(conn)
    runtime.tick(conn)

    parent = dict(get_agent(conn, parent_id))
    assert parent["phase"] == "waiting_children"
    assert '"kind": "waiting_children"' in parent["pending_state_json"]
    assert parent["current_state"] == "watch-ci"

    runtime.tick(conn)
    parent = dict(get_agent(conn, parent_id))
    assert parent["phase"] == "resume_state"
    assert '"kind": "children_wake"' in parent["pending_state_json"]

    runtime.tick(conn)
    wake_prompt = backend.prompts[parent_id][-1]
    assert "Child flows have finished." in wake_prompt
    assert f"agent {child_id} (child) -> success" in wake_prompt
    assert f"/agent-{child_id}/scratchpad.md" in wake_prompt

    runtime.tick(conn)
    parent = dict(get_agent(conn, parent_id))
    assert parent["phase"] == "evaluate_transition"
    assert parent["pending_state_json"] == ""

    runtime.tick(conn)
    runtime.tick(conn)
    parent = dict(get_agent(conn, parent_id))
    assert parent["current_state"] == "done"
    assert parent["ended_at"]

    events = [dict(row) for row in list_agent_events(conn, parent_id)]
    assert [event["kind"] for event in events if event["kind"] in {"wait_children", "wake_children"}] == [
        "wait_children",
        "wake_children",
    ]


def test_runtime_transition_wait_overrides_state_wait(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    conn = connect()
    init_db(conn)
    flow_path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: .

check:
  start: true
  prompt: work
  wait: 10m
  transitions:
    - if: retry later
      wait: 15m
      go: check
    - go: done

done:
  end: true
""".strip(),
    )
    base = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    now_box = {"value": base}
    monkeypatch.setattr("flow.runtime.utc_now", lambda: now_box["value"])

    agent_id = create_runtime_agent(conn, flow_path, {})
    backend = FakeBackend()
    backend.set_script(agent_id, ['worked', '{"choice":"check","reason":"retry later"}'])
    runtime = Runtime(backend=backend)

    runtime.tick(conn)
    agent = dict(get_agent(conn, agent_id))
    assert agent["phase"] == "waiting"
    assert agent["ready_at"] == format_utc(base + timedelta(minutes=10))

    now_box["value"] = base + timedelta(minutes=10, seconds=1)
    runtime.tick(conn)
    runtime.tick(conn)
    runtime.tick(conn)
    runtime.tick(conn)
    runtime.tick(conn)
    agent = dict(get_agent(conn, agent_id))
    assert agent["current_state"] == "check"
    assert agent["phase"] == "waiting"
    assert agent["ready_at"] == format_utc(now_box["value"] + timedelta(minutes=15))


def test_runtime_tick_survives_backend_session_error(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    conn = connect()
    init_db(conn)
    flow_path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: .

check:
  start: true
  prompt: work
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    agent_id = create_runtime_agent(conn, flow_path, {})

    class BrokenBackend(FakeBackend):
        def ensure_session(self, agent: dict[str, Any]) -> dict[str, str]:
            raise RuntimeError("codex readiness probe failed")

    runtime = Runtime(backend=BrokenBackend())
    runtime.tick(conn)

    agent = dict(get_agent(conn, agent_id))
    events = [dict(row) for row in list_agent_events(conn, agent_id)]
    assert agent["substate"] == "needs_help"
    assert agent["phase"] == "paused"
    assert agent["last_error"] == "codex readiness probe failed"
    assert agent["status_message"] == "Needs help"
    assert events[-1]["kind"] == "needs_help"
    assert events[-1]["reason"] == "codex readiness probe failed"


def test_runtime_tick_survives_prompt_submission_error(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    conn = connect()
    init_db(conn)
    flow_path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: .

check:
  start: true
  prompt: work
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    agent_id = create_runtime_agent(conn, flow_path, {})

    class BrokenBackend(FakeBackend):
        def send_prompt(self, agent: dict[str, Any], prompt: str, *, request_id: str = "") -> TurnObservation:
            del agent, prompt, request_id
            raise RuntimeError("prompt submission was not acknowledged")

    runtime = Runtime(backend=BrokenBackend())
    runtime.tick(conn)

    agent = dict(get_agent(conn, agent_id))
    events = [dict(row) for row in list_agent_events(conn, agent_id)]
    assert agent["substate"] == "needs_help"
    assert agent["phase"] == "paused"
    assert agent["last_error"] == "prompt submission was not acknowledged"
    assert agent["status_message"] == "Needs help"
    assert events[-1]["kind"] == "needs_help"
    assert events[-1]["reason"] == "prompt submission was not acknowledged"


def test_runtime_tick_survives_codex_auth_submission_error(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    conn = connect()
    init_db(conn)
    flow_path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: .

check:
  start: true
  prompt: work
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    agent_id = create_runtime_agent(conn, flow_path, {})

    class BrokenBackend(FakeBackend):
        def send_prompt(self, agent: dict[str, Any], prompt: str, *, request_id: str = "") -> TurnObservation:
            del agent, prompt, request_id
            raise RuntimeError("Codex authentication failed; run `codex logout` and `codex login`")

    runtime = Runtime(backend=BrokenBackend())
    runtime.tick(conn)

    agent = dict(get_agent(conn, agent_id))
    events = [dict(row) for row in list_agent_events(conn, agent_id)]
    assert agent["substate"] == "needs_help"
    assert agent["phase"] == "paused"
    assert agent["last_error"] == "Codex authentication failed; run `codex logout` and `codex login`"
    assert agent["status_message"] == "Needs help"
    assert events[-1]["kind"] == "needs_help"
    assert events[-1]["reason"] == "Codex authentication failed; run `codex logout` and `codex login`"


def test_runtime_run_forever_retries_transient_database_lock(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    runtime = Runtime(backend=FakeBackend())
    call_count = {"value": 0}

    def fake_tick(_conn: Any) -> None:
        call_count["value"] += 1
        if call_count["value"] == 1:
            raise sqlite3.OperationalError("database is locked")
        runtime._running = False

    monkeypatch.setattr(runtime, "tick", fake_tick)
    monkeypatch.setattr("flow.runtime.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("flow.runtime.signal.signal", lambda *_args, **_kwargs: None)

    assert runtime.run_forever() == 0
    assert call_count["value"] == 2

    conn = connect()
    init_db(conn)
    exit_info = daemon_exit_info(conn)
    assert exit_info["last_exit_kind"] == "clean"
