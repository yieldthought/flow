from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flow.backend import AgentBackend, TurnObservation
from flow.common import format_utc, utc_now
from flow.flowfile import flow_to_dict, load_flow, render_flow
from flow.runtime import Runtime
from flow.store import (
    connect,
    create_agent,
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
        self.turn_counter = 0

    def set_script(self, agent_id: int, outputs: list[str]) -> None:
        self.scripts[agent_id] = list(outputs)

    def ensure_session(self, agent: dict[str, Any]) -> dict[str, str]:
        agent_id = int(agent["id"])
        self.sessions[agent_id] = True
        return {"launch_command": f"fake-launch-{agent_id}"}

    def send_prompt(self, agent: dict[str, Any], prompt: str) -> None:
        agent_id = int(agent["id"])
        self.prompts.setdefault(agent_id, []).append(prompt)

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
    assert backend.prompts[agent_id][0].count("State: check")


def test_runtime_handles_keep_working_then_finishes(tmp_path: Path, monkeypatch: Any) -> None:
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
    backend.set_script(
        agent_id,
        [
            "worked once",
            '{"choice":"keep_working","reason":"more to do"}',
            "worked twice",
            '{"choice":"done","reason":"finished"}',
        ],
    )
    runtime = Runtime(backend=backend)
    for _ in range(8):
        runtime.tick(conn)
    agent = dict(get_agent(conn, agent_id))
    assert agent["current_state"] == "done"
    assert len(backend.prompts[agent_id]) == 4


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
  end: true
""".strip(),
    )
    agent_id = create_runtime_agent(conn, flow_path, {})
    backend = FakeBackend()
    backend.set_script(
        agent_id,
        [
            "worked",
            '{"choice":"needs_help","reason":"blocked"}',
            "resumed work",
            '{"choice":"done","reason":"finished"}',
        ],
    )
    runtime = Runtime(backend=backend)
    for _ in range(4):
        runtime.tick(conn)
    agent = dict(get_agent(conn, agent_id))
    assert agent["substate"] == "needs_help"

    enqueue_command(conn, agent_id, "resume", {})
    conn.commit()
    for _ in range(5):
        runtime.tick(conn)
    agent = dict(get_agent(conn, agent_id))
    assert agent["current_state"] == "done"


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
    assert agent["last_error"] == "codex readiness probe failed"
    assert agent["status_message"] == "codex readiness probe failed"
