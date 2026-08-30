"""Human and JSON Lines reporting for Flow 2.0."""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from typing import Any, TextIO


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
        self._colour = bool(getattr(self.stream, "isatty", lambda: False)()) and not os.environ.get("NO_COLOR")

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
            prefix = f"{self._stamp()} activity  "
            available = max(8, width - len(prefix))
            if len(summary) > available:
                summary = summary[: max(1, available - 3)] + "..."
        self.emit("activity", summary=summary, **fields)

    def diagnostic(self, message: str) -> None:
        print(message, file=self.error_stream, flush=True)

    def _stamp(self) -> str:
        seconds = int(self.elapsed)
        return f"[{seconds // 60:02d}:{seconds % 60:02d}]"

    def _human_line(self, kind: str, fields: dict[str, Any]) -> str:
        label = kind.replace("_", " ")
        colour = {
            "start": "36",
            "state": "34",
            "transition": "35",
            "activity": "90",
            "waiting": "33",
            "needs_help": "33",
            "final": "32" if fields.get("exit_code") == 0 else "31",
            "error": "31",
            "interrupted": "31",
        }.get(kind, "37")
        if self._colour:
            label = f"\x1b[{colour}m{label}\x1b[0m"
        detail = self._detail(kind, fields)
        return f"{self._stamp()} {label:<18} {detail}".rstrip()

    @staticmethod
    def _detail(kind: str, fields: dict[str, Any]) -> str:
        if kind == "start":
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
