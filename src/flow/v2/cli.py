"""Command-line interface for the isolated Flow 2.0 runtime."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any

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


class UsageError(ValueError):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise UsageError(message)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
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
        print(f"flow2: {exc}", file=sys.stderr)
        return EX_USAGE
    except (ScratchpadError, ValueError, OSError) as exc:
        print(f"flow2: {exc}", file=sys.stderr)
        return EX_DATAERR


def _new_run(argv: list[str]) -> int:
    parser = Parser(prog="flow2", add_help=False)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--scratchpad")
    parser.add_argument("flow_file")
    known, flow_argv = parser.parse_known_args(argv)
    invocation_cwd = str(Path.cwd().resolve())
    flow = _load_valid_flow(known.flow_file)
    try:
        values, cwd = parse_arguments(flow, flow_argv, invocation_cwd)
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
    parser = Parser(prog="flow2 resume")
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
    parser = Parser(prog="flow2 _run")
    parser.add_argument("--scratchpad", required=True)
    args = parser.parse_args(argv)
    scratchpad = Path(args.scratchpad).expanduser().resolve()
    try:
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
        print(f"flow2: {exc}", file=sys.stderr)
        return EX_NEEDS_HELP
    except ScratchpadError as exc:
        print(f"flow2: {exc}", file=sys.stderr)
        return EX_DATAERR
    except Exception as exc:
        print(f"flow2: {exc}", file=sys.stderr)
        return EX_RUNTIME


def _inspect(argv: list[str]) -> int:
    parser = Parser(prog="flow2 inspect")
    parser.add_argument("scratchpad")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    path = Path(args.scratchpad).expanduser().resolve()
    metadata, body = read_scratchpad(path)
    payload = {"scratchpad": str(path), "metadata": metadata, "body": body}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Flow:       {metadata.get('flow', '')}")
        print(f"State:      {metadata.get('state', '')}")
        print(f"Phase:      {metadata.get('phase', '')}")
        print(f"Status:     {metadata.get('status', '')}")
        print(f"Exit:       {metadata.get('exit_code', '')}")
        print(f"Thread:     {metadata.get('thread', '')}")
        print(f"Scratchpad: {path}")
    return 0


def _validate(argv: list[str]) -> int:
    parser = Parser(prog="flow2 validate")
    parser.add_argument("files", nargs="+")
    args = parser.parse_args(argv)
    failed = False
    for path in args.files:
        try:
            flow = load_flow(path)
            result = validate_flow(flow)
        except Exception as exc:
            print(f"{path}: error: {exc}", file=sys.stderr)
            failed = True
            continue
        for warning in result.warnings:
            print(f"{path}: warning: {warning}")
        for error in result.errors:
            print(f"{path}: error: {error}", file=sys.stderr)
        failed = failed or bool(result.errors)
        if not result.errors:
            print(f"{path}: valid")
    return EX_DATAERR if failed else 0


def _ps(argv: list[str]) -> int:
    parser = Parser(prog="flow2 ps")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    print_processes(discover_running_flows(), json_output=bool(args.json), stream=sys.stdout)
    return 0


def _catalog(argv: list[str]) -> int:
    parser = Parser(prog="flow2 catalog")
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
            exits = ", ".join(f"{name}={code}" for name, code in flow["exits"].items())
            print(f"{flow['name']}: {flow['path']} [{exits}]")
        for broken in payload["broken"]:
            print(f"broken: {broken['path']}: {'; '.join(broken['errors'])}", file=sys.stderr)
    return EX_DATAERR if payload["broken"] else 0


def _top(argv: list[str]) -> int:
    parser = Parser(prog="flow2 top")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args(argv)
    if args.interval <= 0:
        raise UsageError("--interval must be greater than zero")
    if not sys.stdout.isatty():
        print_processes(discover_running_flows(), json_output=False, stream=sys.stdout)
        return 0
    try:
        while True:
            print("\x1b[2J\x1b[H", end="")
            print(f"Flow 2.0 top  {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            print_processes(discover_running_flows(), json_output=False, stream=sys.stdout)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


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
    print(
        "usage:\n"
        "  flow2 [--json] [--scratchpad FILE] FILE.flow [flow arguments]\n"
        "  flow2 resume SCRATCHPAD [--json]\n"
        "  flow2 inspect SCRATCHPAD [--json]\n"
        "  flow2 validate FILE.flow [...]\n"
        "  flow2 catalog [PATH ...] [--json]\n"
        "  flow2 ps [--json]\n"
        "  flow2 top [--interval SECONDS]"
    )


if __name__ == "__main__":
    raise SystemExit(main())
