"""Human and JSON Lines reporting for Flow 2.0."""

from __future__ import annotations

import json
import shutil
import sys
import time
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


class Reporter:
    def __init__(
        self,
        *,
        json_output: bool = False,
        stream: TextIO | None = None,
        error_stream: TextIO | None = None,
        activity_interval: float = 60.0,
    ) -> None:
        self.json_output = json_output
        self.stream = stream or sys.stdout
        self.error_stream = error_stream or sys.stderr
        self.started = time.monotonic()
        self.activity_interval = activity_interval
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
        if self.json_output:
            print(json.dumps(event, sort_keys=True, separators=(",", ":")), file=self.stream, flush=True)
        else:
            print(self._human_line(kind, fields), file=self.stream, flush=True)
        return event

    def activity(self, text: str, **fields: Any) -> None:
        now = time.monotonic()
        summary = " ".join(text.split())
        if not summary or now - self._last_activity < self.activity_interval:
            return
        self._last_activity = now
        if not self.json_output:
            width = max(20, shutil.get_terminal_size((100, 24)).columns)
            prefix = f"{self._stamp()} {'activity':<{HUMAN_LABEL_WIDTH}} "
            available = max(8, width - len(prefix))
            if len(summary) > available:
                summary = summary[: max(1, available - 3)] + "..."
        self.emit("activity", summary=summary, **fields)

    def diagnostic(self, message: str) -> None:
        print(
            paint(message, PALETTE.accent, enabled=self._error_colour),
            file=self.error_stream,
            flush=True,
        )

    def _stamp(self) -> str:
        seconds = int(self.elapsed)
        return f"[{seconds // 60:02d}:{seconds % 60:02d}]"

    def _human_line(self, kind: str, fields: dict[str, Any]) -> str:
        display_kind = "resume" if kind == "start" and fields.get("resumed") else kind.replace("_", " ")
        label = f"{display_kind:<{HUMAN_LABEL_WIDTH}}"
        colour = {
            "start": PALETTE.accent,
            "state": PALETTE.state,
            "transition": PALETTE.accent,
            "activity": PALETTE.muted,
            "waiting": PALETTE.warn,
            "needs_help": PALETTE.error,
            "final": PALETTE.ok if fields.get("exit_code") == 0 else PALETTE.error,
            "error": PALETTE.error,
            "interrupted": PALETTE.warn,
        }.get(kind, PALETTE.muted)
        label = paint(
            label,
            colour,
            enabled=self._colour,
            bold=kind in {"needs_help", "final", "error"},
        )
        detail = self._detail(kind, fields)
        return f"{self._stamp()} {label} {detail}".rstrip()

    @staticmethod
    def _detail(kind: str, fields: dict[str, Any]) -> str:
        if kind == "start":
            if fields.get("resumed"):
                phase = str(fields.get("phase") or "unknown").replace("_", " ")
                ready_at = str(fields.get("ready_at") or "")
                if phase in {"state wait", "transition wait"} and ready_at:
                    phase = f"waiting until {ready_at}"
                thread = str(fields.get("thread") or "")
                thread_detail = f"; thread {thread[:8]}" if thread else ""
                return f"{fields.get('flow')} at {fields.get('state')} ({phase}{thread_detail})"
            return f"{fields.get('flow')} -> {fields.get('state')}"
        if kind == "state":
            return str(fields.get("state", ""))
        if kind == "transition":
            return f"{fields.get('from_state')} -> {fields.get('to_state')}: {fields.get('reason', '')}"
        if kind == "waiting":
            return f"{fields.get('state')} until {fields.get('ready_at')}"
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
