"""Command-line interface for the isolated Flow 2.0 runtime."""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import select
import shutil
import socket
import subprocess
import sys
import time
import unicodedata
import webbrowser
from pathlib import Path
from typing import Any

try:
    import termios
    import tty
except ImportError:  # pragma: no cover - Windows
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]

from flow.ansi import PALETTE

from .chart import ChartRenderError, write_chart
from .constants import EX_DATAERR, EX_NEEDS_HELP, EX_RUNTIME, EX_USAGE
from .output import Reporter, json_event_line, render_human_event
from .processes import RunningFlow, discover_running_flows, print_processes
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
from .watch import EventJournal


ENTER_ALTERNATE_SCREEN = "\x1b[?1049h"
LEAVE_ALTERNATE_SCREEN = "\x1b[?1049l"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
CLEAR_SCREEN = "\x1b[2J"
CURSOR_HOME = "\x1b[H"
CLEAR_LINE = "\x1b[2K"
CLEAR_TO_END = "\x1b[J"
ANSI_SEQUENCE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


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
        if command == "chat":
            return _chat(arguments[1:])
        if command == "chart":
            return _chart(arguments[1:])
        if command == "inspect":
            return _inspect(arguments[1:])
        if command == "watch":
            return _watch(arguments[1:])
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


def _chat(argv: list[str]) -> int:
    parser = Parser(prog="flow chat")
    parser.add_argument("scratchpad")
    args = parser.parse_args(argv)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise UsageError("flow chat requires an interactive terminal")

    scratchpad = Path(args.scratchpad).expanduser().resolve()
    try:
        with ScratchpadLock(scratchpad):
            metadata, _ = read_scratchpad(scratchpad)
            _validate_resume_environment(metadata, allow_change=False)
            thread = str(metadata.get("thread") or "").strip()
            if not thread:
                raise ScratchpadError(f"scratchpad has no Codex thread: {scratchpad}")

            executable = shutil.which("codex")
            if not executable:
                _print_error("cannot start chat: codex executable was not found on PATH")
                return EX_RUNTIME

            cwd, fallback_from = _chat_working_directory(metadata, scratchpad)
            if fallback_from is not None:
                _print_notice(
                    f"original working directory no longer exists: {fallback_from}; using {cwd}"
                )
            try:
                result = subprocess.run(
                    [executable, "resume", "-C", str(cwd), thread],
                    cwd=str(cwd),
                    check=False,
                )
            except OSError as exc:
                _print_error(f"cannot start chat: {exc}")
                return EX_RUNTIME
            finally:
                repair_scratchpad(scratchpad, metadata)
            return result.returncode if result.returncode >= 0 else 128 - result.returncode
    except ScratchpadLockedError:
        _print_error(f"cannot start chat while the scratchpad is in use: {scratchpad}")
        return EX_NEEDS_HELP


def _chart(argv: list[str]) -> int:
    parser = Parser(prog="flow chart", description="Render a Flow definition as a standalone HTML chart.")
    parser.add_argument("flow_file", help="Flow definition to visualize")
    parser.add_argument(
        "-o",
        "--output",
        metavar="FILE",
        help="write FILE without opening a viewer (default: temporary file and default browser)",
    )
    parser.add_argument(
        "--theme",
        choices=("dark", "light"),
        default="dark",
        help="chart colour theme (default: dark)",
    )
    args = parser.parse_args(argv)
    if args.output and Path(args.output).suffix.lower() not in {".html", ".htm"}:
        raise UsageError("--output must name an .html or .htm file")
    flow = _load_valid_flow(args.flow_file)
    try:
        path = write_chart(flow, args.output, theme=args.theme)
    except ChartRenderError as exc:
        _print_error(str(exc))
        return EX_RUNTIME

    _print_field("Chart", path, PALETTE.accent, bold=True)
    if args.output:
        return 0
    try:
        opened = _open_chart(path)
    except (OSError, webbrowser.Error) as exc:
        _print_error(f"chart was written to {path}, but the viewer could not be opened: {exc}")
        return EX_RUNTIME
    if not opened:
        _print_error(f"chart was written to {path}, but no platform viewer accepted it")
        return EX_RUNTIME
    return 0


def _open_chart(path: Path) -> bool:
    if sys.platform == "darwin":
        result = subprocess.run(
            ["/usr/bin/open", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            detail = " ".join(result.stderr.split()) or f"open exited {result.returncode}"
            raise OSError(detail)
        return True

    if os.name == "nt":  # pragma: no cover - exercised on Windows
        getattr(os, "startfile")(str(path))
        return True

    opener = shutil.which("xdg-open")
    if opener:
        result = subprocess.run(
            [opener, str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            detail = " ".join(result.stderr.split()) or f"xdg-open exited {result.returncode}"
            raise OSError(detail)
        return True

    return webbrowser.open_new_tab(path.as_uri())


def _internal_run(argv: list[str]) -> int:
    parser = Parser(prog="flow _run")
    parser.add_argument("--scratchpad", required=True)
    args = parser.parse_args(argv)
    scratchpad = Path(args.scratchpad).expanduser().resolve()
    json_output = False
    try:
        metadata, _ = read_scratchpad(scratchpad)
        json_output = bool(metadata.get("json"))
        with ScratchpadLock(scratchpad) as lock:
            metadata, _ = read_scratchpad(scratchpad)
            _validate_resume_environment(metadata, allow_change=False)
            flow = _load_valid_flow(str(metadata["flow_path"]))
            if flow.digest != metadata.get("flow_digest"):
                raise ScratchpadError("flow file changed since this run")
            values = metadata.get("arguments") or {}
            if not isinstance(values, dict):
                raise ScratchpadError("scratchpad arguments must be a mapping")
            rendered = render_flow(flow, {str(key): str(value) for key, value in values.items()}, str(metadata["cwd"]))
            reporter = Reporter(json_output=json_output, event_sink=lock.append_event)
            runtime = FlowRuntime(rendered, scratchpad, metadata, reporter=reporter)
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


def _watch(argv: list[str]) -> int:
    parser = Parser(prog="flow watch", colour="--json" not in argv)
    parser.add_argument("scratchpad")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--interval", type=float, default=0.2)
    args = parser.parse_args(argv)
    if args.interval <= 0:
        raise UsageError("--interval must be greater than zero")
    scratchpad = Path(args.scratchpad).expanduser().resolve()
    metadata, _ = read_scratchpad(scratchpad)
    stream = sys.stdout
    use_colour = not args.json and colour_enabled(stream)
    input_fd, input_attributes = (
        _enable_top_input() if not args.json and stream.isatty() else (None, None)
    )
    missing_since = time.monotonic()
    try:
        with EventJournal(scratchpad) as journal:
            while True:
                for event in journal.poll():
                    _print_watch_event(event, json_output=bool(args.json), colour=use_colour, stream=stream)
                final = journal.final_event
                if final is not None:
                    return _event_exit_code(final)

                metadata, _ = read_scratchpad(scratchpad)
                if journal.live_journal_closed:
                    _print_error("live output journal ended without a final event", plain=bool(args.json))
                    _print_watch_snapshot(
                        metadata,
                        scratchpad=str(scratchpad),
                        json_output=bool(args.json),
                        colour=use_colour,
                        stream=stream,
                    )
                    return _metadata_exit_code(metadata, EX_RUNTIME)
                if journal.path == journal.persistent_path and str(metadata.get("status") or "") != "running":
                    _print_error("recorded output has no final event", plain=bool(args.json))
                    _print_watch_snapshot(
                        metadata,
                        scratchpad=str(scratchpad),
                        json_output=bool(args.json),
                        colour=use_colour,
                        stream=stream,
                    )
                    return _metadata_exit_code(metadata, EX_RUNTIME)
                if journal.stream is None and time.monotonic() - missing_since >= 1.0:
                    _print_error(
                        "no recorded output is available; showing the current checkpoint",
                        plain=bool(args.json),
                    )
                    _print_watch_snapshot(
                        metadata,
                        scratchpad=str(scratchpad),
                        json_output=bool(args.json),
                        colour=use_colour,
                        stream=stream,
                    )
                    default = EX_NEEDS_HELP if str(metadata.get("status") or "") == "running" else 0
                    return _metadata_exit_code(metadata, default)

                key = _wait_for_top_key(input_fd, float(args.interval))
                if key in {"q", "Q", "\x1b", "\x1b[D"}:
                    return 0
    except KeyboardInterrupt:
        return 0
    finally:
        _restore_top_input(input_fd, input_attributes)


def _print_watch_event(
    event: dict[str, Any],
    *,
    json_output: bool,
    colour: bool,
    stream: Any,
    clip: bool = False,
) -> None:
    if json_output:
        print(json_event_line(event), file=stream, flush=True)
        return
    width = max(20, shutil.get_terminal_size((100, 24)).columns)
    print(render_human_event(event, colour=colour, width=width, clip=clip), file=stream, flush=True)


def _print_watch_snapshot(
    metadata: dict[str, Any],
    *,
    scratchpad: str,
    json_output: bool,
    colour: bool,
    stream: Any,
) -> None:
    event = _watch_snapshot_event(metadata, scratchpad=scratchpad)
    _print_watch_event(event, json_output=json_output, colour=colour, stream=stream)


def _watch_snapshot_event(metadata: dict[str, Any], *, scratchpad: str) -> dict[str, Any]:
    exit_code = metadata.get("exit_code")
    kind = "final" if isinstance(exit_code, int) else "state"
    return {
        "event": kind,
        "timestamp": metadata.get("updated_at", ""),
        "elapsed_seconds": 0.0,
        "flow": metadata.get("flow", ""),
        "state": metadata.get("state", ""),
        "phase": metadata.get("phase", ""),
        "exit_code": exit_code,
        "scratchpad": scratchpad,
        "thread": metadata.get("thread", ""),
        "resumable": metadata.get("resumable", False),
        "outcome": metadata.get("last_outcome", ""),
        "error": metadata.get("last_error", ""),
    }


def _event_exit_code(event: dict[str, Any]) -> int:
    try:
        return int(event.get("exit_code") or 0)
    except (TypeError, ValueError):
        return EX_RUNTIME


def _metadata_exit_code(metadata: dict[str, Any], default: int) -> int:
    value = metadata.get("exit_code")
    return int(value) if isinstance(value, int) else default


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
    selected_scratchpad = ""
    selected_index = 0
    view = "list"
    watched: RunningFlow | None = None
    journal: EventJournal | None = None
    scroll = 0
    try:
        stream.write(ENTER_ALTERNATE_SCREEN + HIDE_CURSOR + CLEAR_SCREEN + CURSOR_HOME)
        stream.flush()
        while True:
            items = discover_running_flows()
            if view == "list":
                selected_scratchpad, selected_index = _normalise_top_selection(
                    items,
                    selected_scratchpad,
                    selected_index,
                )
                frame = _render_top_list(items, selected_scratchpad, use_colour)
            else:
                assert journal is not None and watched is not None
                journal.poll()
                frame, scroll = _render_top_watch(watched, journal, scroll, use_colour)
            stream.write(CURSOR_HOME + _redraw_top_frame(frame.getvalue()) + CLEAR_TO_END)
            stream.flush()
            key = _wait_for_top_key(input_fd, args.interval)
            if key in {"q", "Q"}:
                return 0
            if view == "list":
                if key == "\x1b":
                    return 0
                if key in {"\x1b[A", "k"} and items:
                    selected_index = max(0, selected_index - 1)
                    selected_scratchpad = items[selected_index].scratchpad
                elif key in {"\x1b[B", "j"} and items:
                    selected_index = min(len(items) - 1, selected_index + 1)
                    selected_scratchpad = items[selected_index].scratchpad
                elif key in {"\x1b[C", "\r", "\n"} and selected_scratchpad:
                    watched = next((item for item in items if item.scratchpad == selected_scratchpad), None)
                    if watched is not None:
                        journal = EventJournal(watched.scratchpad)
                        journal.poll()
                        scroll = 0
                        view = "watch"
            else:
                if key in {"\x1b", "\x1b[D"}:
                    journal.close()
                    journal = None
                    watched = None
                    scroll = 0
                    view = "list"
                elif key in {"\x1b[A", "k"}:
                    scroll += 1
                elif key in {"\x1b[B", "j"}:
                    scroll = max(0, scroll - 1)
    except KeyboardInterrupt:
        return 0
    finally:
        if journal is not None:
            journal.close()
        _restore_top_input(input_fd, input_attributes)
        stream.write(SHOW_CURSOR + LEAVE_ALTERNATE_SCREEN)
        stream.flush()


def _normalise_top_selection(items: list[RunningFlow], scratchpad: str, index: int) -> tuple[str, int]:
    if not items:
        return "", 0
    for current_index, item in enumerate(items):
        if item.scratchpad == scratchpad:
            return scratchpad, current_index
    bounded = min(max(0, index), len(items) - 1)
    return items[bounded].scratchpad, bounded


def _redraw_top_frame(text: str) -> str:
    columns = max(1, shutil.get_terminal_size((120, 30)).columns)
    rows = text.splitlines()
    return "".join(
        f"{CLEAR_LINE}\r{_clip_terminal_line(row, columns)}\n"
        for row in rows
    ) + "\r"


def _clip_terminal_line(text: str, width: int) -> str:
    visible = ANSI_SEQUENCE.sub("", text)
    if _terminal_width(visible) <= width:
        return text
    marker = "..." if width >= 3 else "." * width
    limit = max(0, width - len(marker))
    output: list[str] = []
    used = 0
    position = 0
    coloured = False
    stopped = False
    for match in ANSI_SEQUENCE.finditer(text):
        used, stopped = _append_terminal_cells(text[position : match.start()], output, used, limit)
        if stopped:
            break
        output.append(match.group(0))
        coloured = True
        position = match.end()
    if not stopped:
        _append_terminal_cells(text[position:], output, used, limit)
    clipped = "".join(output) + marker
    return clipped + "\x1b[0m" if coloured else clipped


def _append_terminal_cells(text: str, output: list[str], used: int, limit: int) -> tuple[int, bool]:
    for character in text:
        cells = _character_width(character, used)
        if used + cells > limit:
            return used, True
        output.append(character)
        used += cells
    return used, False


def _terminal_width(text: str) -> int:
    width = 0
    for character in text:
        width += _character_width(character, width)
    return width


def _character_width(character: str, current_width: int) -> int:
    if character == "\t":
        return 8 - (current_width % 8)
    category = unicodedata.category(character)
    if unicodedata.combining(character) or category.startswith("M"):
        return 0
    if category in {"Cc", "Cf"}:
        return 0
    return 2 if unicodedata.east_asian_width(character) in {"F", "W"} else 1


def _render_top_list(items: list[RunningFlow], selected_scratchpad: str, use_colour: bool) -> io.StringIO:
    frame = io.StringIO()
    title = paint("Flow top", PALETTE.bright, enabled=use_colour, bold=True)
    timestamp = paint(time.strftime("%Y-%m-%d %H:%M:%S"), PALETTE.subtle, enabled=use_colour)
    print(f"{title}  {timestamp}\n", file=frame)
    print_processes(
        items,
        json_output=False,
        stream=frame,
        colour=use_colour,
        selected_scratchpad=selected_scratchpad or None,
    )
    if items:
        controls = "up/down select  enter/right watch  q/esc quit"
        print("\n" + paint(controls, PALETTE.dim, enabled=use_colour), file=frame)
    return frame


def _render_top_watch(
    watched: RunningFlow,
    journal: EventJournal,
    scroll: int,
    use_colour: bool,
) -> tuple[io.StringIO, int]:
    frame = io.StringIO()
    columns, rows = shutil.get_terminal_size((120, 30))
    try:
        metadata, _ = read_scratchpad(watched.scratchpad)
    except ScratchpadError:
        metadata = {}
    flow_name = str(metadata.get("flow") or watched.flow)
    state = str(metadata.get("state") or watched.state)
    phase = str(metadata.get("phase") or watched.phase).replace("_", " ")
    status = str(metadata.get("status") or "running")
    timestamp_text = time.strftime("%Y-%m-%d %H:%M:%S")
    show_timestamp = columns >= len(timestamp_text) + 20
    title_width = columns - len(timestamp_text) - 2 if show_timestamp else columns
    title_text = _clip_text(f"Flow watch  {flow_name}", max(1, title_width))
    title = paint(title_text, PALETTE.bright, enabled=use_colour, bold=True)
    timestamp = paint(timestamp_text, PALETTE.subtle, enabled=use_colour)
    print(f"{title}  {timestamp}" if show_timestamp else title, file=frame)
    summary = _clip_text(f"{state}  {phase}  {status}  pid {watched.pid}", columns)
    print(paint(summary, PALETTE.state, enabled=use_colour, bold=True), file=frame)
    print(paint(_clip_text(watched.scratchpad, columns), PALETTE.subtle, enabled=use_colour), file=frame)
    print(file=frame)

    events = list(journal.events)
    if not events and isinstance(metadata.get("exit_code"), int):
        events.append(_watch_snapshot_event(metadata, scratchpad=watched.scratchpad))
    lines = [
        render_human_event(event, colour=use_colour, width=columns, clip=True)
        for event in events
    ]
    body_height = max(1, rows - 7)
    max_scroll = max(0, len(lines) - body_height)
    scroll = min(max(0, scroll), max_scroll)
    end = len(lines) - scroll
    start = max(0, end - body_height)
    visible = lines[start:end]
    if not visible:
        visible = [paint("Waiting for the first event...", PALETTE.subtle, enabled=use_colour)]
    for line in visible:
        print(line, file=frame)
    for _ in range(max(0, body_height - len(visible))):
        print(file=frame)

    follow = "following" if scroll == 0 else f"{scroll} line{'s' if scroll != 1 else ''} above latest"
    controls = f"up/down scroll  left/esc back  q quit  [{follow}]"
    print(paint(_clip_text(controls, columns), PALETTE.dim, enabled=use_colour), file=frame)
    return frame, scroll


def _clip_text(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: max(1, width - 3)] + "..."


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
        first = os.read(file_descriptor, 1)
        if first != b"\x1b":
            return first.decode("utf-8", errors="ignore")
        readable, _, _ = select.select([file_descriptor], [], [], 0.02)
        suffix = os.read(file_descriptor, 7) if readable else b""
        return (first + suffix).decode("utf-8", errors="ignore")
    except (OSError, ValueError):
        time.sleep(timeout)
        return ""


def _load_valid_flow(path: str):
    source = Path(path).expanduser().resolve()
    if source.suffix != ".flow":
        raise ValueError(f"Flow files must use the .flow extension: {source}")
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


def _chat_working_directory(metadata: dict[str, Any], scratchpad: Path) -> tuple[Path, Path | None]:
    original = Path(str(metadata.get("cwd") or "")).expanduser()
    if original.is_dir():
        return original.resolve(), None
    invocation = Path(str(metadata.get("invocation_cwd") or "")).expanduser()
    if invocation.is_dir():
        return invocation.resolve(), original
    return scratchpad.parent, original


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
        " chat SCRATCHPAD",
        " chart FILE.flow [-o OUTPUT.html] [--theme dark|light]",
        " inspect SCRATCHPAD [--json]",
        " watch SCRATCHPAD [--json] [--interval SECONDS]",
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


def _print_notice(message: str) -> None:
    enabled = colour_enabled(sys.stderr)
    prefix = paint("flow chat:", PALETTE.warn, enabled=enabled, bold=True)
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
