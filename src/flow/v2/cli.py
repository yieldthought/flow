"""Command-line interface for the isolated Flow 2.0 runtime."""

from __future__ import annotations

import asyncio
import io
import json
import os
import select
import socket
import sys
import time
from pathlib import Path
from typing import Any

try:
    import termios
    import tty
except ImportError:  # pragma: no cover - Windows
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]

from flow.ansi import PALETTE

from .constants import EX_DATAERR, EX_NEEDS_HELP, EX_RUNTIME, EX_USAGE
from .processes import discover_running_flows, print_processes
from .runtime import FlowRuntime
from .scratchpad import (
    ScratchpadError,
    ScratchpadLock,
    ScratchpadLockedError,
    codex_home,
    create_scratchpad,
    default_scratchpad,
    new_metadata,
    read_scratchpad,
    repair_scratchpad,
)
from .spec import discover_catalog, load_flow, parse_arguments, render_flow, validate_flow
from .style import StyledArgumentParser, colour_enabled, paint, phase_colour, status_colour


ENTER_ALTERNATE_SCREEN = "\x1b[?1049h"
LEAVE_ALTERNATE_SCREEN = "\x1b[?1049l"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
CLEAR_SCREEN = "\x1b[2J"
CURSOR_HOME = "\x1b[H"
CLEAR_TO_END = "\x1b[J"


class UsageError(ValueError):
    pass


class Parser(StyledArgumentParser):
    def error(self, message: str) -> None:
        raise UsageError(message)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in arguments
    if not arguments:
        _print_usage()
        return EX_USAGE
    command = arguments[0]
    try:
        if command == "resume":
            return _resume(arguments[1:])
        if command == "inspect":
            return _inspect(arguments[1:])
        if command == "validate":
            return _validate(arguments[1:])
        if command == "catalog":
            return _catalog(arguments[1:])
        if command == "ps":
            return _ps(arguments[1:])
        if command == "top":
            return _top(arguments[1:])
        if command == "_run":
            return _internal_run(arguments[1:])
        if command in {"-h", "--help", "help"}:
            _print_usage()
            return 0
        return _new_run(arguments)
    except UsageError as exc:
        _print_error(str(exc), plain=json_mode)
        return EX_USAGE
    except (ScratchpadError, ValueError, OSError) as exc:
        _print_error(str(exc), plain=json_mode)
        return EX_DATAERR


def _new_run(argv: list[str]) -> int:
    parser = Parser(prog="flow", add_help=False, colour="--json" not in argv)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--scratchpad")
    parser.add_argument("flow_file")
    known, flow_argv = parser.parse_known_args(argv)
    invocation_cwd = str(Path.cwd().resolve())
    flow = _load_valid_flow(known.flow_file)
    try:
        values, cwd = parse_arguments(flow, flow_argv, invocation_cwd, colour_output=not known.json)
    except SystemExit as exc:
        return 0 if exc.code == 0 else EX_USAGE
    rendered = render_flow(flow, values, cwd)
    scratchpad = (
        Path(known.scratchpad).expanduser().resolve()
        if known.scratchpad
        else default_scratchpad(flow.source_path, invocation_cwd)
    )
    metadata = new_metadata(
        flow_path=flow.source_path,
        flow_digest=flow.digest,
        flow_name=rendered.name,
        argv=argv,
        arguments=values,
        invocation_cwd=invocation_cwd,
        cwd=cwd,
        state=rendered.start_state,
        json_output=bool(known.json),
    )
    create_scratchpad(scratchpad, metadata)
    return _exec_internal(scratchpad)


def _resume(argv: list[str]) -> int:
    parser = Parser(prog="flow resume", colour="--json" not in argv)
    parser.add_argument("scratchpad")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--allow-changed-flow", action="store_true")
    parser.add_argument("--allow-environment-change", action="store_true")
    args = parser.parse_args(argv)
    scratchpad = Path(args.scratchpad).expanduser().resolve()
    metadata, _ = read_scratchpad(scratchpad)
    if metadata.get("status") == "completed" or metadata.get("phase") == "completed":
        raise ScratchpadError(f"flow is already complete: {scratchpad}")
    _validate_resume_environment(metadata, allow_change=bool(args.allow_environment_change))
    if args.allow_environment_change:
        metadata["host"] = socket.gethostname()
        metadata["codex_home"] = codex_home()
    flow = _load_valid_flow(str(metadata["flow_path"]))
    if flow.digest != metadata.get("flow_digest") and not args.allow_changed_flow:
        raise ScratchpadError("flow file changed since this run; pass --allow-changed-flow to resume deliberately")
    if args.allow_changed_flow:
        metadata["flow_digest"] = flow.digest
    if args.json:
        metadata["json"] = True
    repair_scratchpad(scratchpad, metadata)
    return _exec_internal(scratchpad)


def _internal_run(argv: list[str]) -> int:
    parser = Parser(prog="flow _run")
    parser.add_argument("--scratchpad", required=True)
    args = parser.parse_args(argv)
    scratchpad = Path(args.scratchpad).expanduser().resolve()
    json_output = False
    try:
        metadata, _ = read_scratchpad(scratchpad)
        json_output = bool(metadata.get("json"))
        with ScratchpadLock(scratchpad):
            metadata, _ = read_scratchpad(scratchpad)
            _validate_resume_environment(metadata, allow_change=False)
            flow = _load_valid_flow(str(metadata["flow_path"]))
            if flow.digest != metadata.get("flow_digest"):
                raise ScratchpadError("flow file changed since this run")
            values = metadata.get("arguments") or {}
            if not isinstance(values, dict):
                raise ScratchpadError("scratchpad arguments must be a mapping")
            rendered = render_flow(flow, {str(key): str(value) for key, value in values.items()}, str(metadata["cwd"]))
            runtime = FlowRuntime(rendered, scratchpad, metadata)
            return asyncio.run(runtime.run())
    except ScratchpadLockedError as exc:
        _print_error(str(exc), plain=json_output)
        return EX_NEEDS_HELP
    except ScratchpadError as exc:
        _print_error(str(exc), plain=json_output)
        return EX_DATAERR
    except Exception as exc:
        _print_error(str(exc), plain=json_output)
        return EX_RUNTIME


def _inspect(argv: list[str]) -> int:
    parser = Parser(prog="flow inspect", colour="--json" not in argv)
    parser.add_argument("scratchpad")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    path = Path(args.scratchpad).expanduser().resolve()
    metadata, body = read_scratchpad(path)
    payload = {"scratchpad": str(path), "metadata": metadata, "body": body}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        exit_code = metadata.get("exit_code")
        exit_value = str(exit_code)
        exit_colour = PALETTE.ok if exit_code == 0 else PALETTE.error if isinstance(exit_code, int) else PALETTE.muted
        _print_field("Flow", metadata.get("flow", ""), PALETTE.bright, bold=True)
        _print_field("State", metadata.get("state", ""), PALETTE.state, bold=True)
        _print_field("Phase", metadata.get("phase", ""), phase_colour(str(metadata.get("phase") or "")))
        _print_field("Status", metadata.get("status", ""), status_colour(str(metadata.get("status") or "")))
        _print_field("Exit", exit_value, exit_colour, bold=isinstance(exit_code, int))
        _print_field("Thread", metadata.get("thread", ""), PALETTE.muted)
        _print_field("Scratchpad", path, PALETTE.subtle)
    return 0


def _validate(argv: list[str]) -> int:
    parser = Parser(prog="flow validate")
    parser.add_argument("files", nargs="+")
    args = parser.parse_args(argv)
    failed = False
    for path in args.files:
        try:
            flow = load_flow(path)
            result = validate_flow(flow)
        except Exception as exc:
            _print_validation(path, "error", str(exc), stream=sys.stderr)
            failed = True
            continue
        for warning in result.warnings:
            _print_validation(path, "warning", warning, stream=sys.stdout)
        for error in result.errors:
            _print_validation(path, "error", error, stream=sys.stderr)
        failed = failed or bool(result.errors)
        if not result.errors:
            _print_validation(path, "valid", stream=sys.stdout)
    return EX_DATAERR if failed else 0


def _ps(argv: list[str]) -> int:
    parser = Parser(prog="flow ps", colour="--json" not in argv)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    print_processes(
        discover_running_flows(),
        json_output=bool(args.json),
        stream=sys.stdout,
        colour=not args.json and colour_enabled(sys.stdout),
    )
    return 0


def _catalog(argv: list[str]) -> int:
    parser = Parser(prog="flow catalog", colour="--json" not in argv)
    parser.add_argument("paths", nargs="*")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    entries = discover_catalog(args.paths or None)
    payload: dict[str, Any] = {"flows": [], "broken": []}
    for entry in entries:
        if entry.flow is None or entry.validation is None or not entry.validation.ok:
            errors = [entry.error] if entry.error else list(entry.validation.errors if entry.validation else ())
            payload["broken"].append({"path": entry.path, "errors": errors})
            continue
        payload["flows"].append(
            {
                "name": entry.flow.name,
                "path": entry.path,
                "description": entry.flow.description or "",
                "args": {
                    name: {"help": spec.help, "default": spec.default}
                    for name, spec in entry.flow.args.items()
                },
                "exits": {
                    state.name: state.exit_code
                    for state in entry.flow.states.values()
                    if state.terminal
                },
            }
        )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for flow in payload["flows"]:
            _print_catalog_flow(flow)
        for broken in payload["broken"]:
            _print_catalog_broken(broken)
    return EX_DATAERR if payload["broken"] else 0


def _top(argv: list[str]) -> int:
    parser = Parser(prog="flow top")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args(argv)
    if args.interval <= 0:
        raise UsageError("--interval must be greater than zero")
    if not sys.stdout.isatty():
        print_processes(discover_running_flows(), json_output=False, stream=sys.stdout)
        return 0
    stream = sys.stdout
    use_colour = colour_enabled(stream)
    input_fd, input_attributes = _enable_top_input()
    try:
        stream.write(ENTER_ALTERNATE_SCREEN + HIDE_CURSOR + CLEAR_SCREEN + CURSOR_HOME)
        stream.flush()
        while True:
            frame = io.StringIO()
            title = paint("Flow 2.0 top", PALETTE.bright, enabled=use_colour, bold=True)
            timestamp = paint(time.strftime("%Y-%m-%d %H:%M:%S"), PALETTE.subtle, enabled=use_colour)
            print(f"{title}  {timestamp}\n", file=frame)
            print_processes(discover_running_flows(), json_output=False, stream=frame, colour=use_colour)
            stream.write(CURSOR_HOME + frame.getvalue() + CLEAR_TO_END)
            stream.flush()
            if _wait_for_top_key(input_fd, args.interval) in {"q", "Q", "\x1b"}:
                return 0
    except KeyboardInterrupt:
        return 0
    finally:
        _restore_top_input(input_fd, input_attributes)
        stream.write(SHOW_CURSOR + LEAVE_ALTERNATE_SCREEN)
        stream.flush()


def _enable_top_input() -> tuple[int | None, list[Any] | None]:
    if termios is None or tty is None or not sys.stdin.isatty():
        return None, None
    try:
        file_descriptor = sys.stdin.fileno()
        attributes = termios.tcgetattr(file_descriptor)
        tty.setcbreak(file_descriptor)
        return file_descriptor, attributes
    except (AttributeError, OSError, ValueError):
        return None, None


def _restore_top_input(file_descriptor: int | None, attributes: list[Any] | None) -> None:
    if file_descriptor is None or attributes is None or termios is None:
        return
    try:
        termios.tcsetattr(file_descriptor, termios.TCSADRAIN, attributes)
    except (OSError, ValueError):
        pass


def _wait_for_top_key(file_descriptor: int | None, timeout: float) -> str:
    if file_descriptor is None:
        time.sleep(timeout)
        return ""
    try:
        readable, _, _ = select.select([file_descriptor], [], [], timeout)
        if not readable:
            return ""
        return os.read(file_descriptor, 1).decode("utf-8", errors="ignore")
    except (OSError, ValueError):
        time.sleep(timeout)
        return ""


def _load_valid_flow(path: str):
    source = Path(path).expanduser().resolve()
    if source.suffix != ".flow":
        raise ValueError(f"Flow 2.0 files must use the .flow extension: {source}")
    flow = load_flow(source)
    result = validate_flow(flow)
    if result.errors:
        raise ValueError("invalid flow: " + "; ".join(result.errors))
    return flow


def _validate_resume_environment(metadata: dict[str, Any], *, allow_change: bool) -> None:
    if allow_change:
        return
    if metadata.get("host") != socket.gethostname():
        raise ScratchpadError(
            f"scratchpad belongs to host {metadata.get('host')!r}; resume it there or pass --allow-environment-change"
        )
    if str(metadata.get("codex_home") or "") != codex_home():
        raise ScratchpadError(
            f"scratchpad belongs to CODEX_HOME {metadata.get('codex_home')!r}; resume with that environment"
        )


def _exec_internal(scratchpad: Path) -> int:
    argv = [sys.executable, "-m", "flow.v2.cli", "_run", "--scratchpad", str(scratchpad)]
    os.execv(sys.executable, argv)
    raise AssertionError("os.execv returned")


def _print_usage() -> None:
    enabled = colour_enabled(sys.stdout)
    print(paint("usage:", PALETTE.bright, enabled=enabled, bold=True))
    suffixes = (
        " [--json] [--scratchpad FILE] FILE.flow [flow arguments]",
        " resume SCRATCHPAD [--json]",
        " inspect SCRATCHPAD [--json]",
        " validate FILE.flow [...]",
        " catalog [PATH ...] [--json]",
        " ps [--json]",
        " top [--interval SECONDS]",
    )
    for suffix in suffixes:
        command = paint("flow", PALETTE.accent, enabled=enabled, bold=True)
        print(f"  {command}{paint(suffix, PALETTE.subtle, enabled=enabled)}")


def _print_error(message: str, *, plain: bool = False) -> None:
    enabled = not plain and colour_enabled(sys.stderr)
    prefix = paint("flow:", PALETTE.error, enabled=enabled, bold=True)
    print(f"{prefix} {message}", file=sys.stderr)


def _print_field(label: str, value: Any, value_colour: int, *, bold: bool = False) -> None:
    enabled = colour_enabled(sys.stdout)
    key = paint(f"{label}:".ljust(12), PALETTE.muted, enabled=enabled)
    rendered = paint(str(value), value_colour, enabled=enabled, bold=bold)
    print(f"{key}{rendered}")


def _print_validation(path: str, status: str, message: str = "", *, stream: Any) -> None:
    enabled = colour_enabled(stream)
    status_code = {"valid": PALETTE.ok, "warning": PALETTE.warn, "error": PALETTE.error}[status]
    path_text = paint(str(path), PALETTE.subtle, enabled=enabled)
    status_text = paint(status, status_code, enabled=enabled, bold=status != "warning")
    suffix = f": {message}" if message else ""
    print(f"{path_text}: {status_text}{suffix}", file=stream)


def _print_catalog_flow(flow: dict[str, Any]) -> None:
    enabled = colour_enabled(sys.stdout)
    name = paint(str(flow["name"]), PALETTE.bright, enabled=enabled, bold=True)
    path = paint(str(flow["path"]), PALETTE.subtle, enabled=enabled)
    exits = ", ".join(
        f"{paint(str(state), PALETTE.state, enabled=enabled)}="
        f"{paint(str(code), PALETTE.ok if code == 0 else PALETTE.error, enabled=enabled, bold=code != 0)}"
        for state, code in flow["exits"].items()
    )
    print(f"{name}: {path} [{exits}]")


def _print_catalog_broken(broken: dict[str, Any]) -> None:
    enabled = colour_enabled(sys.stderr)
    label = paint("broken:", PALETTE.error, enabled=enabled, bold=True)
    path = paint(str(broken["path"]), PALETTE.subtle, enabled=enabled)
    print(f"{label} {path}: {'; '.join(broken['errors'])}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
