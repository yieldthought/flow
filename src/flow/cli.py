"""CLI entrypoint."""

from __future__ import annotations

import argparse
import json
import os
import select
import shutil
import sqlite3
import subprocess
import sys
import termios
import tempfile
import time
import tty
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

import yaml

from . import __version__
from .backend import CodexBackend
from .common import current_actor, format_utc, parse_wait_seconds, pending_state_payload, to_json, utc_now
from .flowfile import discover_catalog, flow_to_dict, load_flow, parse_start_arguments, render_flow, validate_flow
from .paths import ensure_home, logs_dir
from .render import fit_list_top, fit_show_top, fit_top_dashboard, render_list, render_show, render_top_dashboard
from .runtime import Runtime
from .scratchpad import ensure_scratchpad_dir, scratchpad_path_text
from .store import (
    connect,
    create_agent,
    daemon_status,
    enqueue_command,
    get_agent,
    get_flow_snapshot,
    list_agent_events,
    list_top_agent_events,
    list_top_agents,
    get_meta,
    init_db,
    list_agents,
    record_flow_snapshot,
    set_meta,
    transaction,
    update_agent,
)
from .ui_server import start_ui_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flow")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("files", nargs="+")

    catalog_parser = subparsers.add_parser("catalog")
    catalog_parser.add_argument("--format", choices=("yaml", "json", "text"), default="yaml")
    catalog_parser.add_argument("--broken", action="store_true")

    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("file")
    start_parser.add_argument("state", nargs="?")
    start_parser.add_argument("args", nargs=argparse.REMAINDER)

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("flow_name", nargs="?")
    list_parser.add_argument("--top", action="store_true")

    show_parser = subparsers.add_parser("show")
    show_parser.add_argument("agent_id")
    show_parser.add_argument("--top", action="store_true")
    show_parser.add_argument("--json", action="store_true")

    top_parser = subparsers.add_parser("top")
    top_parser.add_argument("flow_name", nargs="?")
    top_parser.add_argument(
        "--recent",
        default="1h",
        help="include agents finished within this duration (default: 1h)",
    )

    ui_parser = subparsers.add_parser("ui")
    ui_parser.add_argument("flow_name", nargs="?")

    for name in ("pause", "interrupt", "resume", "wake", "delete"):
        command = subparsers.add_parser(name)
        command.add_argument("agent_id")

    view_parser = subparsers.add_parser("view")
    view_parser.add_argument("agent_ids", nargs="*", metavar="AGENT_ID", help="one or more live agent ids to display")
    view_parser.add_argument("--all", action="store_true", help="display all live agent sessions in a tiled view")

    stop_parser = subparsers.add_parser("stop")
    stop_parser.add_argument("agent_id")
    stop_parser.add_argument("state", nargs="?")

    move_parser = subparsers.add_parser("move", help="move an agent to a state and leave it paused until resume")
    move_parser.add_argument("agent_id")
    move_parser.add_argument("state")

    subparsers.add_parser("init")
    subparsers.add_parser("restart")
    subparsers.add_parser("self-test")

    shutdown_parser = subparsers.add_parser("shutdown")
    shutdown_parser.add_argument("tokens", nargs="*")

    daemon_parser = subparsers.add_parser("_daemon")
    daemon_parser.add_argument("--foreground", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ensure_home()
    conn = connect()

    if args.command == "_daemon":
        init_db(conn)
        runtime = Runtime()
        return runtime.run_forever()
    if args.command == "validate":
        return cmd_validate(conn, list(args.files))
    if args.command == "catalog":
        return cmd_catalog(conn, output_format=str(args.format), include_broken=bool(args.broken))
    if args.command == "list":
        init_db(conn)
        return cmd_list(conn, args.flow_name, top=bool(args.top))

    init_db(conn)
    if args.command == "show":
        return cmd_show(conn, int(args.agent_id), top=bool(args.top), json_output=bool(args.json))
    if args.command == "top":
        return cmd_top(conn, args.flow_name, recent=str(args.recent))
    if args.command == "ui":
        return cmd_ui(conn, args.flow_name)
    if args.command == "init":
        return cmd_init(conn)
    if args.command == "restart":
        return cmd_restart(conn)
    if args.command == "self-test":
        return cmd_self_test(conn)
    if args.command == "start":
        return cmd_start(conn, args.file, args.state, list(args.args))
    if args.command == "interrupt":
        return cmd_queue_and_wait(conn, int(args.agent_id), "interrupt", {})
    if args.command == "pause":
        return cmd_queue_and_wait(conn, int(args.agent_id), "pause", {})
    if args.command == "resume":
        ensure_daemon(conn)
        return cmd_queue_and_wait(conn, int(args.agent_id), "resume", {})
    if args.command == "wake":
        ensure_daemon(conn)
        return cmd_queue_and_wait(conn, int(args.agent_id), "wake", {})
    if args.command == "view":
        return cmd_view(conn, list(args.agent_ids), view_all=bool(args.all))
    if args.command == "move":
        ensure_daemon(conn)
        return cmd_queue_and_wait(conn, int(args.agent_id), "move", {"state": args.state})
    if args.command == "stop":
        ensure_daemon(conn)
        payload = {"state": args.state} if args.state else {}
        return cmd_queue_and_wait(conn, int(args.agent_id), "stop", payload)
    if args.command == "delete":
        return cmd_delete(conn, int(args.agent_id))
    if args.command == "shutdown":
        return cmd_shutdown(conn, list(args.tokens))
    raise AssertionError(f"unhandled command {args.command}")


def cmd_validate(conn: Any, paths: str | list[str]) -> int:
    del conn
    path_list = [paths] if isinstance(paths, str) else list(paths)
    show_path = len(path_list) > 1
    failed = False

    for path in path_list:
        try:
            flow = load_flow(path)
        except Exception as exc:
            print(f"{path}: error: {exc}", file=sys.stderr)
            failed = True
            continue
        result = validate_flow(flow)
        for warning in result.warnings:
            print(f"{path}: warning: {warning}")
        if result.errors:
            for error in result.errors:
                print(f"{path}: error: {error}", file=sys.stderr)
            failed = True
            continue
        if show_path:
            print(f"{path}: flow file is valid")
        else:
            print("flow file is valid")
    return 1 if failed else 0


def cmd_catalog(conn: Any, *, output_format: str = "yaml", include_broken: bool = False) -> int:
    del conn
    catalog = discover_catalog()
    flows_payload: list[dict[str, Any]] = []
    for item in catalog.flows:
        entry: dict[str, Any] = {"name": item.name}
        if item.description:
            entry["description"] = item.description
        entry["path"] = item.path
        entry["args"] = dict(item.args)
        entry["end_states"] = list(item.end_states)
        flows_payload.append(entry)
    payload: dict[str, Any] = {"flows": flows_payload}
    if include_broken:
        payload["broken"] = [{"path": item.path, "error": item.error} for item in catalog.broken]

    if output_format == "json":
        print(json.dumps(payload, indent=2, sort_keys=False))
        return 0
    if output_format == "text":
        print(_render_catalog_text(payload))
        return 0
    print(yaml.safe_dump(payload, sort_keys=False).strip())
    return 0


def cmd_init(conn: Any) -> int:
    status = daemon_status(conn)
    if status["active"] == "1":
        print(f"runtime already active (pid {status['pid']})")
        return 0
    return 0 if ensure_daemon(conn) else 1


def cmd_restart(conn: Any) -> int:
    status = daemon_status(conn)
    if status["active"] == "1":
        stop_result = cmd_shutdown(conn, [])
        if stop_result != 0:
            return stop_result
    return cmd_init(conn)


def cmd_start(conn: Any, path: str, state_token: str | None, extra: list[str]) -> int:
    try:
        agent_id, start_state, warnings = _create_agent_from_flow_file(conn, path, state_token, extra)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    for warning in warnings:
        print(f"warning: {warning}")

    if not ensure_daemon(conn):
        print("error: failed to start runtime daemon", file=sys.stderr)
        return 1

    print(f"started agent #{agent_id} in state '{start_state}'")
    return 0


_SELF_TEST_FLOW = """
flow:
  name: flow-self-test
  description: Short harmless end-to-end Codex integration check.
  mode: workspace-write
  thinking: low
  fast: false

check:
  start: true
  prompt: |
    This is a flow self-test.

    Do not use any tools. Do not read or write files.
    Reply with exactly:
    FLOW_SELF_TEST_OK

    When the runtime later asks for a transition choice, choose `success`.
  transitions:
    - if: the exact self-test response was completed and the integration is working
      go: success
    - if: anything went wrong
      go: failure

success:
  end: true

failure:
  end: true
""".strip()


def cmd_self_test(conn: Any, *, timeout: float = 120.0) -> int:
    work_path = Path(tempfile.mkdtemp(prefix="flow-self-test-"))
    keep_workdir = False
    try:
        flow_path = work_path / "flow-self-test.yaml"
        flow_path.write_text(_SELF_TEST_FLOW + "\n", encoding="utf-8")

        try:
            agent_id, start_state, warnings = _create_agent_from_flow_file(
                conn,
                str(flow_path),
                None,
                ["--path", str(work_path)],
            )
        except Exception as exc:
            print(f"error: failed to create self-test agent: {exc}", file=sys.stderr)
            return 1

        for warning in warnings:
            print(f"warning: {warning}")

        if not ensure_daemon(conn):
            keep_workdir = True
            print("error: failed to start runtime daemon", file=sys.stderr)
            print(f"self-test agent #{agent_id} was left in place with workdir {work_path}", file=sys.stderr)
            return 1

        print(f"flow self-test: started agent #{agent_id} in state '{start_state}'")
        ok, message, _agent, elapsed = _wait_for_self_test_completion(conn, agent_id, timeout=timeout)
        if not ok:
            keep_workdir = True
            print(f"flow self-test failed after {elapsed:.1f}s: {message}", file=sys.stderr)
            print(f"self-test agent #{agent_id} was left in place with workdir {work_path}", file=sys.stderr)
            return 1

        cleanup_error = _delete_self_test_agent(conn, agent_id)
        if cleanup_error:
            keep_workdir = True
            print(
                f"warning: self-test passed but cleanup failed for agent #{agent_id}: {cleanup_error}; "
                f"workdir left at {work_path}",
                file=sys.stderr,
            )
        print(f"flow self-test passed in {elapsed:.1f}s")
        return 0
    finally:
        if not keep_workdir:
            shutil.rmtree(work_path, ignore_errors=True)


def _create_agent_from_flow_file(
    conn: Any,
    path: str,
    state_token: str | None,
    extra: list[str],
) -> tuple[int, str, tuple[str, ...]]:
    flow = load_flow(path)
    result = validate_flow(flow)
    if result.errors:
        raise ValueError("; ".join(result.errors))

    start_state, values, cwd = parse_start_arguments(flow, state_token, extra)
    rendered = render_flow(flow, values, cwd_override=cwd)
    snapshot_id = record_flow_snapshot(conn, rendered, to_json(flow_to_dict(rendered)))
    args_json = json.dumps(values, sort_keys=True)
    agent_id = create_agent(
        conn,
        flow_snapshot_id=snapshot_id,
        flow_name=rendered.name,
        source_path=rendered.source_path,
        backend="codex",
        start_state=start_state,
        cwd=rendered.path or cwd,
        mode=rendered.mode or "yolo",
        thinking=rendered.thinking or "xhigh",
        args_json=args_json,
        fast=bool(rendered.fast),
    )
    ensure_scratchpad_dir({"id": agent_id})
    update_agent(conn, agent_id, launch_marker=f"flow-agent-{agent_id}-{uuid.uuid4().hex[:8]}")
    conn.commit()
    return agent_id, start_state, tuple(result.warnings)


def _wait_for_self_test_completion(
    conn: Any,
    agent_id: int,
    *,
    timeout: float,
) -> tuple[bool, str, dict[str, Any] | None, float]:
    started = time.monotonic()
    deadline = started + timeout
    last_agent: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        row = get_agent(conn, agent_id)
        if row is None:
            return False, "agent disappeared before completing", None, time.monotonic() - started
        agent = dict(row)
        last_agent = agent
        if agent.get("substate") == "needs_help":
            detail = str(agent.get("last_error") or agent.get("status_message") or "agent needs help")
            return False, detail, agent, time.monotonic() - started
        if agent.get("ended_at"):
            if agent.get("current_state") == "success" and agent.get("phase") == "finished":
                return True, "success", agent, time.monotonic() - started
            state = str(agent.get("current_state") or "?")
            status = str(agent.get("status_message") or "").strip()
            detail = f"agent ended in state '{state}'"
            if status:
                detail += f": {status}"
            return False, detail, agent, time.monotonic() - started
        time.sleep(0.5)

    detail = "timed out waiting for success"
    if last_agent is not None:
        detail += (
            f" (state={last_agent.get('current_state')}, phase={last_agent.get('phase')}, "
            f"substate={last_agent.get('substate')})"
        )
    return False, detail, last_agent, time.monotonic() - started


def _delete_self_test_agent(conn: Any, agent_id: int) -> str:
    try:
        result = cmd_delete(conn, agent_id)
    except Exception as exc:
        return str(exc)
    if result != 0:
        return f"delete command exited with status {result}"
    return ""


def cmd_queue_and_wait(conn: Any, agent_id: int, kind: str, payload: dict[str, Any]) -> int:
    if get_agent(conn, agent_id) is None:
        print(f"error: unknown agent {agent_id}", file=sys.stderr)
        return 1
    if not ensure_daemon(conn):
        print("error: failed to start runtime daemon", file=sys.stderr)
        return 1
    command_id = enqueue_command(conn, agent_id, kind, payload, actor=current_actor(), source="cli")
    conn.commit()
    error = wait_for_command(conn, command_id)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


def cmd_list(conn: Any, flow_name: str | None, *, top: bool = False) -> int:
    def render_once() -> str:
        rows = [dict(row) for row in list_agents(conn, flow_name)]
        return render_list(conn, rows)

    if top:
        return run_top_mode(
            render_once,
            fitter=fit_list_top,
            on_exit=lambda: _mark_list_seen(conn),
        )

    print(render_once())
    _mark_list_seen(conn)
    return 0


def cmd_show(conn: Any, agent_id: int, *, top: bool = False, json_output: bool = False) -> int:
    row = get_agent(conn, agent_id)
    if row is None:
        print(f"error: unknown agent {agent_id}", file=sys.stderr)
        return 1
    if top and json_output:
        print("error: --json and --top cannot be combined", file=sys.stderr)
        return 1

    def render_once() -> str:
        current = get_agent(conn, agent_id)
        if current is None:
            return f"error: unknown agent {agent_id}"
        events = [dict(item) for item in list_agent_events(conn, agent_id)]
        if json_output:
            return json.dumps(_show_json_snapshot(conn, dict(current), events), indent=2, sort_keys=False)
        return render_show(conn, dict(current), events)

    if top:
        return run_top_mode(render_once, fitter=fit_show_top)

    print(render_once())
    return 0


def cmd_top(conn: Any, flow_name: str | None, *, recent: str = "1h") -> int:
    try:
        recent_seconds = parse_wait_seconds(recent)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    def render_once() -> str:
        cutoff = format_utc(utc_now() - timedelta(seconds=recent_seconds))
        agents = [dict(row) for row in list_top_agents(conn, flow_name=flow_name, ended_after=cutoff)]
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
        event_limit = max(200, shutil.get_terminal_size(fallback=(80, 24)).lines * 20) if interactive else None
        events = [
            dict(row)
            for row in list_top_agent_events(conn, flow_name=flow_name, ended_after=cutoff, limit=event_limit)
        ]
        return render_top_dashboard(conn, agents, events)

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(render_once())
        _mark_list_seen(conn)
        return 0

    return run_top_mode(render_once, fitter=fit_top_dashboard, on_exit=lambda: _mark_list_seen(conn))


def cmd_ui(conn: Any, flow_name: str | None) -> int:
    ui_dir = Path(__file__).resolve().parents[2] / "ui"
    package_json = ui_dir / "package.json"
    if not package_json.exists():
        print(f"error: UI workspace not found at {ui_dir}", file=sys.stderr)
        return 1
    if shutil.which("npm") is None:
        print("error: npm is required for 'flow ui'", file=sys.stderr)
        return 1

    handle = start_ui_server()
    env = dict(os.environ)
    env["FLOW_UI_API_BASE_URL"] = handle.url
    env["FLOW_UI_FLOW_NAME"] = flow_name or ""
    env["VITE_FLOW_UI_API_BASE_URL"] = handle.url
    env["VITE_FLOW_UI_FLOW_NAME"] = flow_name or ""
    cargo_bin = str(Path.home() / ".cargo" / "bin")
    if Path(cargo_bin).exists():
        env["PATH"] = f"{cargo_bin}:{env.get('PATH', '')}"
    command = [
        "npm",
        "run",
        "tauri:dev",
    ]
    try:
        return subprocess.call(command, cwd=ui_dir, env=env)
    finally:
        handle.close()


def cmd_view(conn: Any, agent_ids: list[str], *, view_all: bool = False) -> int:
    if view_all and agent_ids:
        print("error: use either explicit agent ids or --all", file=sys.stderr)
        return 1
    if not view_all and not agent_ids:
        print("error: specify one or more agent ids or use --all", file=sys.stderr)
        return 1

    backend = CodexBackend()
    if view_all:
        agents = [dict(row) for row in list_agents(conn) if not row["ended_at"]]
        if not agents:
            print("error: no live agents to view", file=sys.stderr)
            return 1
    else:
        agents = []
        seen: set[int] = set()
        for token in agent_ids:
            try:
                agent_id = int(token)
            except ValueError:
                print(f"error: invalid agent id '{token}'", file=sys.stderr)
                return 1
            if agent_id in seen:
                continue
            seen.add(agent_id)
            row = get_agent(conn, agent_id)
            if row is None:
                print(f"error: unknown agent {agent_id}", file=sys.stderr)
                return 1
            if row["ended_at"]:
                print(f"error: agent {agent_id} is already in an end state", file=sys.stderr)
                return 1
            agents.append(dict(row))

    missing = [str(agent["id"]) for agent in agents if not backend.session_exists(agent)]
    if missing:
        joined = ", ".join(missing)
        print(f"error: no live tmux session for agent(s): {joined}", file=sys.stderr)
        return 1

    if len(agents) == 1:
        return backend.attach(agents[0])
    return backend.attach_many(agents)


def cmd_delete(conn: Any, agent_id: int) -> int:
    row = get_agent(conn, agent_id)
    if row is None:
        print(f"error: unknown agent {agent_id}", file=sys.stderr)
        return 1
    agent = dict(row)
    if not ensure_daemon(conn):
        print("error: failed to start runtime daemon", file=sys.stderr)
        return 1
    if not agent["ended_at"]:
        stop_result = cmd_queue_and_wait(conn, agent_id, "stop", {})
        if stop_result != 0:
            return stop_result
    command_id = enqueue_command(conn, agent_id, "delete", {})
    conn.commit()
    error = wait_for_agent_absent(conn, agent_id, command_id)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


def cmd_shutdown(conn: Any, tokens: list[str]) -> int:
    flow_name = ""
    mode = "graceful"
    if tokens:
        if tokens[0] == "now":
            mode = "now"
        else:
            flow_name = tokens[0]
            if len(tokens) > 1:
                if tokens[1] != "now":
                    print("error: shutdown syntax is 'flow shutdown [flow] [now]'", file=sys.stderr)
                    return 1
                mode = "now"
        if len(tokens) > 2:
            print("error: shutdown syntax is 'flow shutdown [flow] [now]'", file=sys.stderr)
            return 1

    status = daemon_status(conn)
    if status["active"] != "1":
        print("runtime is already shut down")
        return 0

    with transaction(conn):
        set_meta(conn, "shutdown_mode", mode)
        set_meta(conn, "shutdown_flow", flow_name)
        set_meta(conn, "shutdown_requested_at", format_utc(utc_now()))

    error = wait_for_shutdown(conn, flow_name, stop_daemon=not flow_name)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


def ensure_daemon(conn: Any) -> bool:
    status = daemon_status(conn)
    if status["active"] == "1":
        return True
    log_path = logs_dir() / "daemon.log"
    with log_path.open("a", encoding="utf-8") as handle:
        subprocess.Popen(
            [sys.executable, "-m", "flow.cli", "_daemon", "--foreground"],
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=handle,
            start_new_session=True,
        )
    deadline = time.monotonic() + 5.0
    # A concurrent launcher may have won the runtime's singleton lock. Poll
    # shared daemon status for the full startup window even if this child exits
    # so the winning daemon has time to announce itself.
    while time.monotonic() < deadline:
        time.sleep(0.1)
        status = daemon_status(conn)
        if status["active"] == "1":
            return True
    return False


def wait_for_command(conn: Any, command_id: int, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = conn.execute("SELECT processed_at, error_text FROM commands WHERE id=?", (command_id,)).fetchone()
        if row is None:
            return ""
        if row["processed_at"]:
            return str(row["error_text"] or "")
        time.sleep(0.1)
    return "timed out waiting for command processing"


def wait_for_agent_absent(conn: Any, agent_id: int, command_id: int, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        agent = get_agent(conn, agent_id)
        if agent is None:
            return ""
        row = conn.execute("SELECT processed_at, error_text FROM commands WHERE id=?", (command_id,)).fetchone()
        if row is not None and row["processed_at"] and row["error_text"]:
            return str(row["error_text"])
        time.sleep(0.1)
    return f"timed out waiting for agent {agent_id} deletion"


def wait_for_shutdown(conn: Any, flow_name: str, *, stop_daemon: bool, timeout: float = 30.0) -> str:
    backend = CodexBackend()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        agents = [dict(row) for row in list_agents(conn, flow_name or None)]
        targeted = [agent for agent in agents if not agent["ended_at"]]
        if all(not backend.session_exists(agent) for agent in targeted):
            if not stop_daemon:
                return ""
            status = daemon_status(conn)
            if status["active"] != "1":
                return ""
        time.sleep(0.2)
    target = f" for flow '{flow_name}'" if flow_name else ""
    return f"timed out waiting for runtime shutdown{target}"


def _render_catalog_text(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    flows = payload.get("flows") or []
    if not flows:
        lines.append("No valid flows found.")
    for item in flows:
        if lines:
            lines.append("")
        lines.append(str(item.get("name") or "flow"))
        description = str(item.get("description") or "").strip()
        if description:
            lines.append(f"  {description}")
        lines.append(f"  path: {item.get('path')}")
        args = item.get("args") or {}
        if args:
            lines.append("  args:")
            for name, help_text in args.items():
                text = str(help_text or "").strip() or "(no help text)"
                lines.append(f"    {name}: {text}")
        end_states = item.get("end_states") or []
        lines.append(f"  end_states: {', '.join(str(state) for state in end_states) if end_states else '-'}")
    broken = payload.get("broken") or []
    if broken:
        if lines:
            lines.append("")
        lines.append("Broken:")
        for item in broken:
            lines.append(f"- {item.get('path')}: {item.get('error')}")
    return "\n".join(lines)


def _show_json_snapshot(conn: Any, agent: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    current_state = str(agent["current_state"])
    ended_at = agent.get("ended_at")
    if ended_at:
        if current_state == "stopped":
            end_state: str | None = None
            terminated_reason: str | None = "stopped"
        else:
            end_state = current_state
            terminated_reason = "finished"
    else:
        end_state = None
        terminated_reason = None
    snapshot: dict[str, Any] = {
        "id": int(agent["id"]),
        "flow": str(agent["flow_name"]),
        "state": current_state,
        "phase": _show_json_phase(agent),
        "end_state": end_state,
        "terminated_reason": terminated_reason,
        "ready_at": str(agent.get("ready_at") or "") or None,
        "scratchpad_path": scratchpad_path_text(agent),
        "args": _json_args(agent.get("args_json", "")),
        "latest_event": _latest_event_snapshot(events[-1]) if events else None,
    }
    waiting_on = _waiting_children_snapshot(conn, agent)
    if waiting_on is not None:
        snapshot["waiting_on"] = waiting_on
    return snapshot


def _show_json_phase(agent: dict[str, Any]) -> str:
    if agent.get("ended_at"):
        if str(agent.get("current_state") or "") == "stopped":
            return "stopped"
        return "finished"
    substate = str(agent.get("substate") or "")
    if substate == "needs_help":
        return "needs_help"
    if substate == "interaction":
        return "interaction"
    return str(agent.get("phase") or "")


def _waiting_children_snapshot(conn: Any, agent: dict[str, Any]) -> dict[str, Any] | None:
    phase = _show_json_phase(agent)
    payload = pending_state_payload(agent)
    child_ids = payload.get("child_ids") or []
    if phase != "waiting_children" or not isinstance(child_ids, list):
        return None
    pending: list[int] = []
    finished: list[dict[str, Any]] = []
    for item in child_ids:
        try:
            child_id = int(item)
        except (TypeError, ValueError):
            continue
        row = get_agent(conn, child_id)
        if row is None:
            finished.append({"id": child_id, "end_state": None, "status": "unknown"})
            continue
        child = dict(row)
        if child.get("ended_at"):
            child_state = str(child.get("current_state") or "")
            stopped = child_state == "stopped"
            record: dict[str, Any] = {
                "id": child_id,
                "end_state": None if stopped else (child_state or None),
                "status": "stopped" if stopped else "finished",
            }
            finished.append(record)
            continue
        pending.append(child_id)
    return {"pending": pending, "finished": finished}


def _latest_event_snapshot(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "at": str(event.get("created_at") or ""),
        "kind": str(event.get("kind") or ""),
        "detail": _event_detail(event),
    }


def _event_detail(event: dict[str, Any]) -> str:
    kind = str(event.get("kind") or "")
    state_name = str(event.get("state_name") or event.get("from_state") or "")
    reason = str(event.get("reason") or "").strip()
    payload = pending_state_payload({"pending_state_json": event.get("payload_json", "{}")})
    if kind == "started":
        return f"{state_name} started".strip()
    if kind == "decision":
        target = str(event.get("to_state") or event.get("choice") or "").strip()
        return " ".join(part for part in [state_name or "state", "->", target, reason] if part).strip()
    if kind == "delay":
        wait_value = str(payload.get("wait") or "").strip() or "?"
        return " ".join(part for part in [state_name or "state", "wait for", wait_value] if part).strip()
    if kind == "wait_children":
        pending = payload.get("pending") or []
        labels = ", ".join(f"#{int(item)}" for item in pending if str(item).strip())
        return f"wait for child {labels}".strip()
    if kind == "wake_children":
        return reason or "children finished"
    return reason or kind


def _json_args(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def run_top_mode(
    render_once: Callable[[], str],
    *,
    fitter: Callable[[str, int, int], str],
    on_exit: Callable[[], None] | None = None,
    refresh_seconds: float = 5.0,
) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("error: --top requires an interactive terminal", file=sys.stderr)
        return 1

    with _cbreak_stdin():
        sys.stdout.write("\x1b[?25l")
        sys.stdout.flush()
        try:
            next_refresh = 0.0
            while True:
                now = time.monotonic()
                if now >= next_refresh:
                    _draw_top_frame(render_once, fitter)
                    next_refresh = now + refresh_seconds
                timeout = max(0.0, next_refresh - time.monotonic())
                readable, _, _ = select.select([sys.stdin], [], [], timeout)
                if not readable:
                    continue
                key = sys.stdin.read(1)
                if key.lower() == "q":
                    break
                if key == " ":
                    _draw_top_frame(render_once, fitter)
                    next_refresh = time.monotonic() + refresh_seconds
        finally:
            sys.stdout.write("\x1b[?25h\n")
            sys.stdout.flush()
            if on_exit is not None:
                on_exit()
    return 0


def _draw_top_frame(render_once: Callable[[], str], fitter: Callable[[str, int, int], str]) -> None:
    size = shutil.get_terminal_size(fallback=(80, 24))
    frame = fitter(render_once(), size.lines, size.columns)
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.write(frame)
    sys.stdout.flush()


def _mark_list_seen(conn: Any) -> None:
    try:
        set_meta(conn, "list_last_seen_error_at", format_utc(utc_now()))
        conn.commit()
    except sqlite3.OperationalError as exc:
        conn.rollback()
        if "database is locked" not in str(exc).lower():
            raise


@contextmanager
def _cbreak_stdin() -> Any:
    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
