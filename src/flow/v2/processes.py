"""Host-local discovery for live Flow 2.0 processes."""

from __future__ import annotations

import getpass
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

import psutil

from flow.ansi import PALETTE

from .scratchpad import read_scratchpad, same_process
from .style import paint, phase_colour


@dataclass(frozen=True)
class RunningFlow:
    pid: int
    elapsed_seconds: int
    flow: str
    state: str
    phase: str
    thread: str
    scratchpad: str
    cwd: str
    started_at: str


def discover_running_flows() -> list[RunningFlow]:
    username = getpass.getuser()
    found: list[RunningFlow] = []
    for process in psutil.process_iter(["pid", "username", "cmdline", "create_time"]):
        try:
            if process.info.get("username") != username:
                continue
            scratchpad = _scratchpad_from_cmdline(process.info.get("cmdline") or [])
            if scratchpad is None:
                continue
            metadata, _ = read_scratchpad(scratchpad)
            if metadata.get("status") != "running" or not same_process(metadata, process):
                continue
            elapsed = max(0, int(datetime.now(timezone.utc).timestamp() - process.create_time()))
            found.append(
                RunningFlow(
                    pid=process.pid,
                    elapsed_seconds=elapsed,
                    flow=str(metadata.get("flow") or Path(str(metadata["flow_path"])).stem),
                    state=str(metadata.get("state") or ""),
                    phase=str(metadata.get("phase") or ""),
                    thread=str(metadata.get("thread") or ""),
                    scratchpad=str(Path(scratchpad).resolve()),
                    cwd=str(metadata.get("cwd") or ""),
                    started_at=str(metadata.get("started_at") or ""),
                )
            )
        except (psutil.Error, OSError, ValueError):
            continue
    return sorted(found, key=lambda item: (item.flow, item.pid))


def print_processes(
    items: list[RunningFlow],
    *,
    json_output: bool,
    stream: TextIO,
    colour: bool = False,
    selected_scratchpad: str | None = None,
) -> None:
    if json_output:
        print(json.dumps({"flows": [asdict(item) for item in items]}, sort_keys=True), file=stream)
        return
    if not items:
        message = paint("No running flows.", PALETTE.subtle, enabled=colour)
        print(message, file=stream)
        return
    rows = [
        ("PID", "ELAPSED", "FLOW", "STATE", "PHASE", "THREAD", "SCRATCHPAD"),
        *[
            (
                str(item.pid),
                _duration(item.elapsed_seconds),
                item.flow,
                item.state,
                item.phase,
                item.thread[:8] if item.thread else "-",
                item.scratchpad,
            )
            for item in items
        ],
    ]
    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
    selected_row = next(
        (index for index, item in enumerate(items, start=1) if item.scratchpad == selected_scratchpad),
        None,
    )
    if colour:
        _print_coloured_processes(rows, widths, stream, selected_row=selected_row)
        return
    for row_index, row in enumerate(rows):
        prefix = "> " if row_index == selected_row else "  " if selected_row is not None else ""
        print(prefix + "  ".join(value.ljust(widths[index]) for index, value in enumerate(row)).rstrip(), file=stream)
        if row_index == 0:
            separator_prefix = "  " if selected_row is not None else ""
            print(separator_prefix + "  ".join("-" * width for width in widths), file=stream)


def _print_coloured_processes(
    rows: list[tuple[str, ...]],
    widths: list[int],
    stream: TextIO,
    *,
    selected_row: int | None = None,
) -> None:
    header = rows[0]
    prefix = "  " if selected_row is not None else ""
    print(
        prefix
        + "  ".join(
            paint(_pad_column(value, index, widths), PALETTE.muted, enabled=True, bold=True)
            for index, value in enumerate(header)
        ),
        file=stream,
    )
    print(
        prefix + "  ".join(paint("-" * width, PALETTE.dim, enabled=True) for width in widths),
        file=stream,
    )
    for row_index, row in enumerate(rows[1:], start=1):
        styles = (
            (PALETTE.accent, True),
            (PALETTE.ok, False),
            (PALETTE.bright, True),
            (PALETTE.state, True),
            (phase_colour(row[4]), False),
            (PALETTE.muted, False),
            (PALETTE.subtle, False),
        )
        print(
            (paint("> ", PALETTE.accent, enabled=True, bold=True) if row_index == selected_row else prefix)
            + "  ".join(
                paint(
                    _pad_column(value, index, widths),
                    styles[index][0],
                    enabled=True,
                    bold=styles[index][1],
                )
                for index, value in enumerate(row)
            ),
            file=stream,
        )


def _pad_column(value: str, index: int, widths: list[int]) -> str:
    return value if index == len(widths) - 1 else value.ljust(widths[index])


def _scratchpad_from_cmdline(cmdline: list[str]) -> str | None:
    if "_run" not in cmdline or "--scratchpad" not in cmdline:
        return None
    try:
        index = cmdline.index("--scratchpad")
        return cmdline[index + 1]
    except (ValueError, IndexError):
        return None


def _duration(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"
