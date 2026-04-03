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
import time
import tty
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from .backend import CodexBackend
from .common import format_utc, to_json, utc_now
from .flowfile import flow_to_dict, load_flow, parse_start_arguments, render_flow, validate_flow
from .paths import ensure_home, logs_dir
from .render import fit_list_top, fit_show_top, render_list, render_show
from .runtime import Runtime
from .store import (
    connect,
    create_agent,
    daemon_status,
    enqueue_command,
    get_agent,
    get_flow_snapshot,
    list_agent_events,
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
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("files", nargs="+")

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

    ui_parser = subparsers.add_parser("ui")
    ui_parser.add_argument("flow_name")

    for name in ("pause", "interrupt", "resume", "wake", "delete"):
        command = subparsers.add_parser(name)
        command.add_argument("agent_id")

    view_parser = subparsers.add_parser("view")
    view_parser.add_argument("agent_ids", nargs="*", metavar="AGENT_ID", help="one or more live agent ids to display")
    view_parser.add_argument("--all", action="store_true", help="display all live agent sessions in a tiled view")

    stop_parser = subparsers.add_parser("stop")
    stop_parser.add_argument("agent_id")
    stop_parser.add_argument("state", nargs="?")

    move_parser = subparsers.add_parser("move")
    move_parser.add_argument("agent_id")
    move_parser.add_argument("state")

    subparsers.add_parser("init")
    subparsers.add_parser("restart")

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
    if args.command == "list":
        init_db(conn)
        return cmd_list(conn, args.flow_name, top=bool(args.top))

    init_db(conn)
    if args.command == "show":
        return cmd_show(conn, int(args.agent_id), top=bool(args.top))
    if args.command == "ui":
        return cmd_ui(conn, args.flow_name)
    if args.command == "init":
        return cmd_init(conn)
    if args.command == "restart":
        return cmd_restart(conn)
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
        flow = load_flow(path)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    result = validate_flow(flow)
    for warning in result.warnings:
        print(f"warning: {warning}")
    if result.errors:
        for error in result.errors:
            print(f"error: {error}", file=sys.stderr)
        return 1

    try:
        start_state, values, cwd = parse_start_arguments(flow, state_token, extra)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

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
    )
    update_agent(conn, agent_id, launch_marker=f"flow-agent-{agent_id}-{uuid.uuid4().hex[:8]}")
    conn.commit()

    if not ensure_daemon(conn):
        print("error: failed to start runtime daemon", file=sys.stderr)
        return 1

    print(f"started agent #{agent_id} in state '{start_state}'")
    return 0


def cmd_queue_and_wait(conn: Any, agent_id: int, kind: str, payload: dict[str, Any]) -> int:
    ensure_daemon(conn)
    if get_agent(conn, agent_id) is None:
        print(f"error: unknown agent {agent_id}", file=sys.stderr)
        return 1
    command_id = enqueue_command(conn, agent_id, kind, payload)
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


def cmd_show(conn: Any, agent_id: int, *, top: bool = False) -> int:
    row = get_agent(conn, agent_id)
    if row is None:
        print(f"error: unknown agent {agent_id}", file=sys.stderr)
        return 1

    def render_once() -> str:
        current = get_agent(conn, agent_id)
        if current is None:
            return f"error: unknown agent {agent_id}"
        events = [dict(item) for item in list_agent_events(conn, agent_id)]
        return render_show(conn, dict(current), events)

    if top:
        return run_top_mode(render_once, fitter=fit_show_top)

    print(render_once())
    return 0


def cmd_ui(conn: Any, flow_name: str) -> int:
    rows = [dict(row) for row in list_agents(conn, flow_name)]
    if not rows:
        print(f"error: unknown or inactive flow '{flow_name}'", file=sys.stderr)
        return 1

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
    env["FLOW_UI_FLOW_NAME"] = flow_name
    env["VITE_FLOW_UI_API_BASE_URL"] = handle.url
    env["VITE_FLOW_UI_FLOW_NAME"] = flow_name
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
    if not agent["ended_at"]:
        ensure_daemon(conn)
        stop_result = cmd_queue_and_wait(conn, agent_id, "stop", {})
        if stop_result != 0:
            return stop_result
    command_id = enqueue_command(conn, agent_id, "delete", {})
    conn.commit()
    wait_for_agent_absent(conn, agent_id, command_id)
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

    wait_for_shutdown(conn, flow_name, stop_daemon=not flow_name)
    return 0


def ensure_daemon(conn: Any) -> bool:
    status = daemon_status(conn)
    if status["active"] == "1":
        return True
    log_path = logs_dir() / "daemon.log"
    with log_path.open("a", encoding="utf-8") as handle:
        process = subprocess.Popen(
            [sys.executable, "-m", "flow.cli", "_daemon", "--foreground"],
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=handle,
            start_new_session=True,
        )
    deadline = time.time() + 5.0
    while time.time() < deadline:
        time.sleep(0.1)
        status = daemon_status(conn)
        if status["active"] == "1":
            return True
        if process.poll() is not None:
            break
    return False


def wait_for_command(conn: Any, command_id: int, timeout: float = 10.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = conn.execute("SELECT processed_at, error_text FROM commands WHERE id=?", (command_id,)).fetchone()
        if row is None:
            return ""
        if row["processed_at"]:
            return str(row["error_text"] or "")
        time.sleep(0.1)
    return "timed out waiting for command processing"


def wait_for_agent_absent(conn: Any, agent_id: int, command_id: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        agent = get_agent(conn, agent_id)
        if agent is None:
            return
        row = conn.execute("SELECT processed_at, error_text FROM commands WHERE id=?", (command_id,)).fetchone()
        if row is not None and row["processed_at"] and row["error_text"]:
            raise RuntimeError(str(row["error_text"]))
        time.sleep(0.1)


def wait_for_shutdown(conn: Any, flow_name: str, *, stop_daemon: bool, timeout: float = 30.0) -> None:
    backend = CodexBackend()
    deadline = time.time() + timeout
    while time.time() < deadline:
        agents = [dict(row) for row in list_agents(conn, flow_name or None)]
        targeted = [agent for agent in agents if not agent["ended_at"]]
        if all(not backend.session_exists(agent) for agent in targeted):
            if not stop_daemon:
                return
            status = daemon_status(conn)
            if status["active"] != "1":
                return
        time.sleep(0.2)


def run_top_mode(
    render_once: Callable[[], str],
    *,
    fitter: Callable[[str, int], str],
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


def _draw_top_frame(render_once: Callable[[], str], fitter: Callable[[str, int], str]) -> None:
    height = shutil.get_terminal_size(fallback=(80, 24)).lines
    frame = fitter(render_once(), height)
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
