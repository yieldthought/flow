"""Human and JSON Lines reporting for Flow 2.0."""

from __future__ import annotations

import json
import shutil
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, TextIO

from flow.ansi import PALETTE

from .style import colour_enabled, paint


HUMAN_EVENT_KINDS = (
    "start",
    "state",
    "transition",
    "activity",
    "waiting",
    "needs_help",
    "final",
    "error",
    "interrupted",
)
HUMAN_LABEL_WIDTH = max(len(kind.replace("_", " ")) for kind in HUMAN_EVENT_KINDS)
EventSink = Callable[[dict[str, Any]], None]


def json_event_line(event: dict[str, Any]) -> str:
    return json.dumps(event, sort_keys=True, separators=(",", ":"))


def elapsed_stamp(seconds: float) -> str:
    total_minutes = max(0, int(seconds)) // 60
    total_hours, minutes = divmod(total_minutes, 60)
    days, hours = divmod(total_hours, 24)
    if days:
        return f"[{days}d {hours:2d}h {minutes:2d}m]"
    if hours:
        return f"[{hours:2d}h {minutes:2d}m]"
    return f"[    {minutes:2d}m]"


def render_human_event(
    event: dict[str, Any],
    *,
    colour: bool = False,
    width: int | None = None,
    clip: bool = False,
) -> str:
    kind = str(event.get("event") or "")
    stamp = elapsed_stamp(float(event.get("elapsed_seconds") or 0))
    display_kind = "resume" if kind == "start" and event.get("resumed") else kind.replace("_", " ")
    visible_label = f"{display_kind:<{HUMAN_LABEL_WIDTH}}"
    detail = event_detail(kind, event)
    if width is not None and (clip or kind == "activity"):
        available = max(8, width - len(stamp) - 1 - HUMAN_LABEL_WIDTH - 1)
        if len(detail) > available:
            detail = detail[: max(1, available - 3)] + "..."
    event_colour = {
        "start": PALETTE.accent,
        "state": PALETTE.state,
        "transition": PALETTE.accent,
        "activity": PALETTE.muted,
        "waiting": PALETTE.warn,
        "needs_help": PALETTE.error,
        "final": PALETTE.ok if event.get("exit_code") == 0 else PALETTE.error,
        "error": PALETTE.error,
        "interrupted": PALETTE.warn,
    }.get(kind, PALETTE.muted)
    label = paint(
        visible_label,
        event_colour,
        enabled=colour,
        bold=kind in {"needs_help", "final", "error"},
    )
    return f"{stamp} {label} {detail}".rstrip()


def event_detail(kind: str, fields: dict[str, Any]) -> str:
    if kind == "start":
        if fields.get("resumed"):
            phase = str(fields.get("phase") or "unknown").replace("_", " ")
            ready_at = str(fields.get("ready_at") or "")
            if phase in {"state wait", "transition wait"} and ready_at:
                phase = f"waiting {_human_wait(ready_at, fields)}"
            thread = str(fields.get("thread") or "")
            thread_detail = f"; thread {thread[:8]}" if thread else ""
            return f"{fields.get('flow')} at {fields.get('state')} ({phase}{thread_detail})"
        return f"{fields.get('flow')} -> {fields.get('state')}"
    if kind == "state":
        return str(fields.get("state", ""))
    if kind == "transition":
        return f"{fields.get('from_state')} -> {fields.get('to_state')}: {fields.get('reason', '')}"
    if kind == "waiting":
        return f"{fields.get('state')} {_human_wait(str(fields.get('ready_at') or ''), fields)}"
    if kind == "activity":
        return str(fields.get("summary", ""))
    if kind == "needs_help":
        return str(fields.get("reason", "needs help"))
    if kind == "interrupted":
        return f"signal {fields.get('signal')}; resumable"
    if kind == "error":
        return str(fields.get("message", ""))
    if kind == "final":
        return f"{fields.get('state')} exit {fields.get('exit_code')} ({fields.get('elapsed_seconds', 0):.1f}s)"
    return " ".join(f"{key}={value}" for key, value in fields.items() if value not in (None, ""))


def _human_wait(ready_at: str, fields: dict[str, Any]) -> str:
    ready = _parse_timestamp(ready_at)
    if ready is None:
        return f"until {ready_at}"

    local = ready.astimezone()
    deadline = f"{local.strftime('%H:%M')} on {local.strftime('%b')} {local.day}"
    started = _parse_timestamp(str(fields.get("timestamp") or ""))
    if started is not None:
        seconds = max(0.0, (ready - started).total_seconds())
        return f"until {deadline} ({_human_duration(seconds)})"
    return f"until {deadline}"


def _parse_timestamp(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _human_duration(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    if total_seconds < 60:
        return f"{total_seconds}s"

    total_minutes = total_seconds // 60
    if total_minutes < 60:
        unit = "min" if total_minutes == 1 else "mins"
        return f"{total_minutes} {unit}"

    total_hours, minutes = divmod(total_minutes, 60)
    if total_hours < 24:
        return f"{total_hours}h" + (f" {minutes}m" if minutes else "")

    days, hours = divmod(total_hours, 24)
    return f"{days}d" + (f" {hours}h" if hours else "")


class Reporter:
    def __init__(
        self,
        *,
        json_output: bool = False,
        stream: TextIO | None = None,
        error_stream: TextIO | None = None,
        activity_interval: float = 60.0,
        event_sink: EventSink | None = None,
    ) -> None:
        self.json_output = json_output
        self.stream = stream or sys.stdout
        self.error_stream = error_stream or sys.stderr
        self.started = time.monotonic()
        self.activity_interval = activity_interval
        self.event_sink = event_sink
        self._last_activity = 0.0
        self._colour = not json_output and colour_enabled(self.stream)
        self._error_colour = not json_output and colour_enabled(self.error_stream)

    @property
    def elapsed(self) -> float:
        return max(0.0, time.monotonic() - self.started)

    def emit(self, kind: str, **fields: Any) -> dict[str, Any]:
        event = {
            "event": kind,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "elapsed_seconds": round(self.elapsed, 3),
            **fields,
        }
        if self.event_sink is not None:
            try:
                self.event_sink(event)
            except (OSError, TypeError, ValueError):
                self.event_sink = None
        if self.json_output:
            print(json_event_line(event), file=self.stream, flush=True)
        else:
            width = max(20, shutil.get_terminal_size((100, 24)).columns) if kind == "activity" else None
            print(render_human_event(event, colour=self._colour, width=width), file=self.stream, flush=True)
        return event

    def activity(self, text: str, **fields: Any) -> None:
        now = time.monotonic()
        summary = " ".join(text.split())
        if not summary or now - self._last_activity < self.activity_interval:
            return
        self._last_activity = now
        self.emit("activity", summary=summary, **fields)

    def diagnostic(self, message: str) -> None:
        print(
            paint(message, PALETTE.accent, enabled=self._error_colour),
            file=self.error_stream,
            flush=True,
        )

    def _human_line(self, kind: str, fields: dict[str, Any]) -> str:
        event = {"event": kind, "elapsed_seconds": self.elapsed, **fields}
        width = max(20, shutil.get_terminal_size((100, 24)).columns) if kind == "activity" else None
        return render_human_event(event, colour=self._colour, width=width)

    @staticmethod
    def _detail(kind: str, fields: dict[str, Any]) -> str:
        return event_detail(kind, fields)
