#!/usr/bin/env python3
"""Standalone stress harness for the real flow runtime/Codex/tmux stack."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
STRESS_DIR = REPO_ROOT / "stress"
DEFAULT_FLOW_BIN = REPO_ROOT / ".venv" / "bin" / "flow"


@dataclass(frozen=True)
class ScenarioSpec:
    name: str
    flow_path: Path
    timeout_seconds: float
    stall_seconds: float = 30.0
    startup_grace_seconds: float = 60.0
    args: tuple[tuple[str, str], ...] = ()


@dataclass
class SnapshotRecord:
    timestamp: str
    agent_id: int
    reason: str
    state: str
    phase: str
    substate: str
    turn_kind: str
    turn_id: str
    request_id: str
    prompt_line: str
    prompt_class: str
    tmux_session: str
    file: str


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def run_cmd(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        env=env,
        check=check,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def parse_agent_id(stdout: str) -> int:
    match = re.search(r"started agent #(\d+)", stdout)
    if not match:
        raise RuntimeError(f"could not parse agent id from flow output:\n{stdout}")
    return int(match.group(1))


def load_rows(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        return list(conn.execute(sql, params))
    finally:
        conn.close()


def get_agent(db_path: Path, agent_id: int) -> sqlite3.Row | None:
    rows = load_rows(db_path, "SELECT * FROM agents WHERE id = ?", (agent_id,))
    return rows[0] if rows else None


def list_agents(db_path: Path) -> list[sqlite3.Row]:
    return load_rows(db_path, "SELECT * FROM agents ORDER BY id")


def list_agent_events(db_path: Path, agent_id: int) -> list[sqlite3.Row]:
    return load_rows(db_path, "SELECT * FROM agent_events WHERE agent_id = ? ORDER BY id", (agent_id,))


def list_commands(db_path: Path) -> list[sqlite3.Row]:
    return load_rows(db_path, "SELECT * FROM commands ORDER BY id")


def tmux_capture(session: str) -> str:
    result = run_cmd(["tmux", "capture-pane", "-pt", f"{session}:0.0", "-S", "-200", "-J"], check=False)
    return result.stdout if result.returncode == 0 else ""


def extract_prompt_line(text: str) -> str:
    for raw_line in reversed(text.splitlines()[-40:]):
        line = raw_line.lstrip()
        if line == "›":
            return ""
        if line.startswith("› "):
            return line[2:].rstrip()
    return ""


def meaningful_lines_from_submitted_prompt(prompt_text: str) -> list[str]:
    lines = [line.rstrip() for line in prompt_text.splitlines()]
    cleaned: list[str] = []
    in_control = False
    for raw in lines:
        stripped = raw.strip()
        if stripped == "[flow-control]":
            in_control = True
            continue
        if stripped == "[/flow-control]":
            in_control = False
            continue
        if in_control or not stripped:
            continue
        cleaned.append(stripped)
    return cleaned[:5]


def read_rollout_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not path.exists() or not path.is_file():
        return events
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
    return events


def rollout_submitted_prompts(events: list[dict[str, Any]]) -> list[tuple[str, list[str]]]:
    submitted: list[tuple[str, list[str]]] = []
    for event in events:
        payload = event.get("payload") or {}
        timestamp = str(event.get("timestamp") or "")
        if event.get("type") == "event_msg" and payload.get("type") == "user_message":
            lines = meaningful_lines_from_submitted_prompt(str(payload.get("message") or ""))
            if lines:
                submitted.append((timestamp, lines))
            continue
        if event.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
            content = payload.get("content") or []
            parts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "input_text":
                    parts.append(str(item.get("text") or ""))
            lines = meaningful_lines_from_submitted_prompt("\n".join(parts))
            if lines:
                submitted.append((timestamp, lines))
    return submitted


def classify_prompt_line(prompt_line: str, submitted_lines: list[str]) -> str:
    stripped = prompt_line.strip()
    if not stripped:
        return "empty"
    if stripped.startswith("/rename"):
        return "rename-command"
    if stripped.startswith("[flow-control]"):
        return "flow-control"
    for line in submitted_lines:
        if not line:
            continue
        prefix = line[: min(80, len(line))]
        if stripped == line or stripped.startswith(prefix):
            return "matches-last-submitted-prompt"
    return "other"


def ensure_codex_home(codex_home: Path) -> None:
    codex_home.mkdir(parents=True, exist_ok=True)
    auth_target = codex_home / "auth.json"
    config_target = codex_home / "config.toml"
    candidates = []
    if os.environ.get("CODEX_HOME"):
        candidates.append(Path(os.environ["CODEX_HOME"]).expanduser())
    candidates.append(Path.home() / ".codex")
    for source_home in candidates:
        if source_home.is_file() or not source_home.exists():
            continue
        auth_source = source_home / "auth.json"
        if auth_source.exists() and not auth_target.exists():
            shutil.copy2(auth_source, auth_target)
        config_source = source_home / "config.toml"
        if config_source.exists() and not config_target.exists():
            shutil.copy2(config_source, config_target)
        if auth_target.exists():
            break


def prepare_env(base_dir: Path, flow_bin: Path) -> dict[str, str]:
    flow_home = base_dir / ".flow"
    codex_home = base_dir / "home" / ".codex"
    ensure_codex_home(codex_home)
    env = dict(os.environ)
    env["FLOW_HOME"] = str(flow_home)
    env["CODEX_HOME"] = str(codex_home)
    env["PATH"] = f"{flow_bin.parent}:{env.get('PATH', '')}"
    return env


def start_scenario(
    flow_bin: Path,
    scenario: ScenarioSpec,
    *,
    scenario_dir: Path,
    env: dict[str, str],
) -> int:
    args = [str(flow_bin), "start", str(scenario.flow_path)]
    for key, value in scenario.args:
        args.extend([f"--{key.replace('_', '-')}", value])
    result = run_cmd(args, cwd=scenario_dir, env=env, check=False)
    (scenario_dir / "start.stdout.txt").write_text(result.stdout, encoding="utf-8")
    (scenario_dir / "start.stderr.txt").write_text(result.stderr, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(
            f"flow start failed for scenario '{scenario.name}' with exit {result.returncode}\n"
            f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
        )
    return parse_agent_id(result.stdout)


def shutdown_runtime(flow_bin: Path, env: dict[str, str]) -> None:
    try:
        run_cmd([str(flow_bin), "shutdown", "now"], cwd=REPO_ROOT, env=env, check=False, timeout=30)
    except subprocess.TimeoutExpired:
        return


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False), encoding="utf-8")


def snapshot_agent(
    scenario_dir: Path,
    agent: sqlite3.Row,
    *,
    reason: str,
    prompt_class_overrides: tuple[str, ...] = (),
) -> SnapshotRecord | None:
    session = str(agent["tmux_session"])
    if not session:
        return None
    pane = tmux_capture(session)
    prompt_line = extract_prompt_line(pane)
    record_time = iso_now()
    file_base = f"{record_time.replace(':', '').replace('-', '')}-agent-{agent['id']}-{reason}"
    pane_path = scenario_dir / "snapshots" / f"{file_base}.txt"
    pane_path.parent.mkdir(parents=True, exist_ok=True)
    pane_path.write_text(pane, encoding="utf-8")
    prompt_class = prompt_class_overrides[0] if prompt_class_overrides else "unclassified"
    return SnapshotRecord(
        timestamp=record_time,
        agent_id=int(agent["id"]),
        reason=reason,
        state=str(agent["current_state"]),
        phase=str(agent["phase"]),
        substate=str(agent["substate"]),
        turn_kind=str(agent["current_turn_kind"]),
        turn_id=str(agent["current_turn_id"]),
        request_id=str(agent["current_request_id"]),
        prompt_line=prompt_line,
        prompt_class=prompt_class,
        tmux_session=session,
        file=str(pane_path),
    )


def monitor_scenario(
    flow_bin: Path,
    scenario: ScenarioSpec,
    *,
    scenario_dir: Path,
    root_agent_id: int,
    env: dict[str, str],
    poll_interval: float,
) -> dict[str, Any]:
    db_path = Path(env["FLOW_HOME"]) / "runtime.sqlite3"
    deadline = time.monotonic() + scenario.timeout_seconds
    startup_deadline = time.monotonic() + scenario.startup_grace_seconds
    known_signatures: dict[int, tuple[Any, ...]] = {}
    snapshots: list[SnapshotRecord] = []
    last_change_at = time.monotonic()
    stalled = False

    while time.monotonic() < deadline:
        if not db_path.exists():
            time.sleep(poll_interval)
            continue
        agents = list_agents(db_path)
        for agent in agents:
            latest_events = list_agent_events(db_path, int(agent["id"]))
            latest_event_id = int(latest_events[-1]["id"]) if latest_events else 0
            signature = (
                agent["current_state"],
                agent["phase"],
                agent["substate"],
                agent["current_turn_kind"],
                agent["current_turn_id"],
                agent["status_message"],
                latest_event_id,
                agent["ended_at"],
            )
            if signature != known_signatures.get(int(agent["id"])):
                snapshot = snapshot_agent(scenario_dir, agent, reason="state-change")
                if snapshot is not None:
                    snapshots.append(snapshot)
                known_signatures[int(agent["id"])] = signature
                last_change_at = time.monotonic()

        root = get_agent(db_path, root_agent_id)
        if root is not None:
            if str(root["ended_at"]):
                active = [agent for agent in agents if not str(agent["ended_at"])]
                if not active:
                    break
            if str(root["substate"]) == "needs_help":
                break
        if (
            scenario.stall_seconds > 0
            and time.monotonic() >= startup_deadline
            and time.monotonic() - last_change_at >= scenario.stall_seconds
        ):
            stalled = True
            break
        time.sleep(poll_interval)

    if db_path.exists():
        agents = list_agents(db_path)
        for agent in agents:
            snapshot = snapshot_agent(scenario_dir, agent, reason="final")
            if snapshot is not None:
                snapshots.append(snapshot)

    shutdown_runtime(flow_bin, env)
    summary = build_scenario_summary(db_path, root_agent_id, snapshots, scenario_dir, stalled=stalled)
    write_json(scenario_dir / "summary.json", summary)
    (scenario_dir / "summary.md").write_text(render_scenario_markdown(summary), encoding="utf-8")
    return summary


def build_scenario_summary(
    db_path: Path,
    root_agent_id: int,
    snapshots: list[SnapshotRecord],
    scenario_dir: Path,
    *,
    stalled: bool,
) -> dict[str, Any]:
    agents_payload: list[dict[str, Any]] = []
    command_rows = list_commands(db_path) if db_path.exists() else []
    command_payload = [dict(row) for row in command_rows]

    snapshot_payload = [dict(snapshot.__dict__) for snapshot in snapshots]
    for agent in list_agents(db_path):
        agent_id = int(agent["id"])
        events = [dict(row) for row in list_agent_events(db_path, agent_id)]
        rollout_path_raw = str(agent["rollout_path"] or "").strip()
        rollout_path = Path(rollout_path_raw) if rollout_path_raw else None
        rollout_events = read_rollout_events(rollout_path) if rollout_path is not None else []
        submitted = rollout_submitted_prompts(rollout_events)
        submitted_lines = submitted[-1][1] if submitted else []
        agent_snapshots: list[dict[str, Any]] = []
        for snapshot in snapshot_payload:
            if snapshot["agent_id"] != agent_id:
                continue
            snapshot = dict(snapshot)
            applicable_lines = submitted_lines
            for submitted_at, lines in submitted:
                if submitted_at and submitted_at <= snapshot["timestamp"]:
                    applicable_lines = lines
            snapshot["prompt_class"] = classify_prompt_line(snapshot["prompt_line"], applicable_lines)
            agent_snapshots.append(snapshot)
        for agent_snapshot in agent_snapshots:
            for original in snapshot_payload:
                if (
                    original["agent_id"] == agent_snapshot["agent_id"]
                    and original["timestamp"] == agent_snapshot["timestamp"]
                    and original["file"] == agent_snapshot["file"]
                ):
                    original["prompt_class"] = agent_snapshot["prompt_class"]
                    break
        agents_payload.append(
            {
                "id": agent_id,
                "flow_name": str(agent["flow_name"]),
                "current_state": str(agent["current_state"]),
                "phase": str(agent["phase"]),
                "substate": str(agent["substate"]),
                "status_message": str(agent["status_message"]),
                "ended_at": str(agent["ended_at"]),
                "tmux_session": str(agent["tmux_session"]),
                "thread_id": str(agent["thread_id"]),
                "rollout_path": str(rollout_path) if rollout_path is not None else "",
                "events": events,
                "rollout_event_count": len(rollout_events),
                "submitted_prompt_lines": submitted_lines,
                "snapshots": agent_snapshots,
            }
        )

    root = next((agent for agent in agents_payload if agent["id"] == root_agent_id), None)
    problematic_prompt_snapshots = [
        snapshot
        for snapshot in snapshot_payload
        if snapshot["prompt_class"] in {"flow-control", "matches-last-submitted-prompt"}
    ]
    return {
        "generated_at": iso_now(),
        "scenario_dir": str(scenario_dir),
        "root_agent_id": root_agent_id,
        "root_flow_name": root["flow_name"] if root else "",
        "root_end_state": root["current_state"] if root and root["phase"] == "finished" else "",
        "root_finished": bool(root and root["phase"] == "finished"),
        "stalled": stalled,
        "agents": agents_payload,
        "commands": command_payload,
        "problematic_prompt_snapshot_count": len(problematic_prompt_snapshots),
        "problematic_prompt_snapshots": problematic_prompt_snapshots,
    }


def render_scenario_markdown(summary: dict[str, Any]) -> str:
    lines = [
        f"# {summary['root_flow_name'] or 'scenario'}",
        "",
        f"- root agent: `{summary['root_agent_id']}`",
        f"- finished: `{summary['root_finished']}`",
        f"- root end state: `{summary['root_end_state']}`",
        f"- stalled: `{summary['stalled']}`",
        f"- agents observed: `{len(summary['agents'])}`",
        f"- problematic prompt snapshots: `{summary['problematic_prompt_snapshot_count']}`",
        "",
        "## Agents",
    ]
    for agent in summary["agents"]:
        lines.extend(
            [
                f"- `#{agent['id']}` `{agent['flow_name']}` state `{agent['current_state']}` phase `{agent['phase']}` substate `{agent['substate']}`",
                f"  status: {agent['status_message'] or '(none)'}",
                f"  events: {', '.join(event['kind'] for event in agent['events'])}",
            ]
        )
    if summary["problematic_prompt_snapshots"]:
        lines.extend(["", "## Problematic Prompt Snapshots"])
        for snapshot in summary["problematic_prompt_snapshots"]:
            lines.append(
                f"- `agent #{snapshot['agent_id']}` `{snapshot['prompt_class']}` at `{snapshot['timestamp']}`: `{snapshot['prompt_line']}`"
            )
    return "\n".join(lines) + "\n"


def local_environment_summary(flow_bin: Path) -> dict[str, Any]:
    codex_version = run_cmd(["codex", "--version"], check=False).stdout.strip()
    tmux_version = run_cmd(["tmux", "-V"], check=False).stdout.strip()
    flow_version = run_cmd([str(flow_bin), "--version"], check=False).stdout.strip()
    return {
        "generated_at": iso_now(),
        "host": platform.node(),
        "platform": platform.platform(),
        "python": sys.version,
        "codex_version": codex_version,
        "tmux_version": tmux_version,
        "flow_version": flow_version,
    }


def scenario_specs(run_root: Path) -> list[ScenarioSpec]:
    child_flow = STRESS_DIR / "child-worker.yaml"
    return [
        ScenarioSpec(
            name="prompt-cycle",
            flow_path=STRESS_DIR / "prompt-cycle.yaml",
            timeout_seconds=240.0,
            stall_seconds=60.0,
            startup_grace_seconds=120.0,
            args=(),
        ),
        ScenarioSpec(
            name="child-handoff",
            flow_path=STRESS_DIR / "child-parent.yaml",
            timeout_seconds=180.0,
            stall_seconds=60.0,
            startup_grace_seconds=120.0,
            args=(("child_flow", str(child_flow)),),
        ),
    ]


def run_suite(args: argparse.Namespace) -> int:
    flow_bin = Path(args.flow_bin).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "environment.json", local_environment_summary(flow_bin))

    selected = set(args.scenario or ["all"])
    specs = scenario_specs(output_dir)
    if "all" not in selected:
        specs = [spec for spec in specs if spec.name in selected]
    if not specs:
        raise SystemExit("no scenarios selected")

    suite_summary: dict[str, Any] = {
        "generated_at": iso_now(),
        "output_dir": str(output_dir),
        "environment": local_environment_summary(flow_bin),
        "scenarios": [],
    }

    for spec in specs:
        for index in range(1, args.repeat + 1):
            scenario_dir = output_dir / f"{spec.name}-run-{index:02d}"
            if scenario_dir.exists():
                shutil.rmtree(scenario_dir)
            scenario_dir.mkdir(parents=True)
            env = prepare_env(scenario_dir, flow_bin)
            resolved_spec = spec
            if spec.name == "child-handoff":
                resolved_spec = ScenarioSpec(
                    name=spec.name,
                    flow_path=spec.flow_path,
                    timeout_seconds=spec.timeout_seconds,
                    stall_seconds=spec.stall_seconds,
                    startup_grace_seconds=spec.startup_grace_seconds,
                    args=spec.args + (("flow_home", str(scenario_dir / ".flow")),),
                )
            root_agent_id = start_scenario(flow_bin, resolved_spec, scenario_dir=scenario_dir, env=env)
            summary = monitor_scenario(
                flow_bin,
                resolved_spec,
                scenario_dir=scenario_dir,
                root_agent_id=root_agent_id,
                env=env,
                poll_interval=args.poll_interval,
            )
            summary["scenario_name"] = spec.name
            summary["run_index"] = index
            suite_summary["scenarios"].append(summary)

    write_json(output_dir / "suite-summary.json", suite_summary)
    (output_dir / "suite-summary.md").write_text(render_suite_markdown(suite_summary), encoding="utf-8")
    return 0 if suite_succeeded(suite_summary) else 1


def render_suite_markdown(summary: dict[str, Any]) -> str:
    env = summary["environment"]
    lines = [
        "# Flow Stress Suite",
        "",
        f"- host: `{env['host']}`",
        f"- platform: `{env['platform']}`",
        f"- codex: `{env['codex_version']}`",
        f"- tmux: `{env['tmux_version']}`",
        f"- flow: `{env['flow_version']}`",
        "",
        "## Scenario Runs",
    ]
    for scenario in summary["scenarios"]:
        lines.append(
            f"- `{scenario['scenario_name']}` run `{scenario['run_index']:02d}`: finished=`{scenario['root_finished']}` "
            f"end_state=`{scenario['root_end_state']}` agents=`{len(scenario['agents'])}` "
            f"problematic_prompt_snapshots=`{scenario['problematic_prompt_snapshot_count']}`"
        )
    return "\n".join(lines) + "\n"


def suite_succeeded(summary: dict[str, Any]) -> bool:
    for scenario in summary["scenarios"]:
        if not scenario["root_finished"]:
            return False
        if scenario["root_end_state"] != "success":
            return False
        if scenario["stalled"]:
            return False
        if scenario["problematic_prompt_snapshot_count"]:
            return False
    return True


def compare_reports(args: argparse.Namespace) -> int:
    left = json.loads(Path(args.left).read_text(encoding="utf-8"))
    right = json.loads(Path(args.right).read_text(encoding="utf-8"))
    lines = [
        "# Flow Stress Comparison",
        "",
        f"- left host: `{left['environment']['host']}` codex `{left['environment']['codex_version']}`",
        f"- right host: `{right['environment']['host']}` codex `{right['environment']['codex_version']}`",
        "",
        "| scenario | left finished | right finished | left prompt issues | right prompt issues |",
        "| --- | --- | --- | --- | --- |",
    ]
    left_map = {(item["scenario_name"], item["run_index"]): item for item in left["scenarios"]}
    right_map = {(item["scenario_name"], item["run_index"]): item for item in right["scenarios"]}
    for key in sorted(set(left_map) | set(right_map)):
        lhs = left_map.get(key)
        rhs = right_map.get(key)
        scenario_name, run_index = key
        lines.append(
            "| "
            + f"{scenario_name}#{run_index:02d} | "
            + f"{lhs['root_finished'] if lhs else '-'} | "
            + f"{rhs['root_finished'] if rhs else '-'} | "
            + f"{lhs['problematic_prompt_snapshot_count'] if lhs else '-'} | "
            + f"{rhs['problematic_prompt_snapshot_count'] if rhs else '-'} |"
        )
    output = "\n".join(lines) + "\n"
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run the stress suite")
    run_parser.add_argument("--output-dir", required=True)
    run_parser.add_argument("--flow-bin", default=str(DEFAULT_FLOW_BIN))
    run_parser.add_argument("--scenario", action="append", choices=["all", "prompt-cycle", "child-handoff"])
    run_parser.add_argument("--repeat", type=int, default=1)
    run_parser.add_argument("--poll-interval", type=float, default=0.5)

    compare_parser = subparsers.add_parser("compare", help="compare two suite-summary.json reports")
    compare_parser.add_argument("left")
    compare_parser.add_argument("right")
    compare_parser.add_argument("--output")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return run_suite(args)
    if args.command == "compare":
        return compare_reports(args)
    raise AssertionError(f"unsupported command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
