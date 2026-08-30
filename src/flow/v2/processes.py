"""Host-local discovery for live Flow 2.0 processes."""

from __future__ import annotations

import getpass
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

import psutil

from .scratchpad import read_scratchpad, same_process


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


def print_processes(items: list[RunningFlow], *, json_output: bool, stream: TextIO) -> None:
    if json_output:
        print(json.dumps({"flows": [asdict(item) for item in items]}, sort_keys=True), file=stream)
        return
    if not items:
        print("No running flows.", file=stream)
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
    for row_index, row in enumerate(rows):
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)).rstrip(), file=stream)
        if row_index == 0:
            print("  ".join("-" * width for width in widths), file=stream)


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
