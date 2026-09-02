"""Read the ephemeral event journal owned by a live Flow process."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, TextIO

from .scratchpad import journal_path


class EventJournal:
    def __init__(self, scratchpad: str | Path) -> None:
        self.scratchpad = Path(scratchpad).expanduser().resolve()
        self.live_path = journal_path(self.scratchpad)
        self.persistent_path = self.scratchpad.with_suffix(".jsonl")
        self.path: Path | None = None
        self.stream: TextIO | None = None
        self.events: list[dict[str, Any]] = []

    @property
    def final_event(self) -> dict[str, Any] | None:
        return next((event for event in reversed(self.events) if event.get("event") == "final"), None)

    @property
    def live(self) -> bool:
        return self.live_path.exists()

    @property
    def live_journal_closed(self) -> bool:
        if self.path != self.live_path or self.stream is None:
            return False
        try:
            opened = os.fstat(self.stream.fileno())
            current = self.live_path.stat()
        except FileNotFoundError:
            return True
        return (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)

    def poll(self) -> list[dict[str, Any]]:
        self._open_if_available()
        if self.stream is None:
            return []
        found: list[dict[str, Any]] = []
        while True:
            position = self.stream.tell()
            line = self.stream.readline()
            if not line:
                break
            if not line.endswith("\n"):
                self.stream.seek(position)
                break
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or not event.get("event"):
                continue
            found.append(event)
        self.events.extend(found)
        return found

    def close(self) -> None:
        if self.stream is not None:
            self.stream.close()
        self.stream = None

    def _open_if_available(self) -> None:
        if self.stream is not None:
            return
        for candidate in (self.live_path, self.persistent_path):
            try:
                self.stream = candidate.open("r", encoding="utf-8")
            except FileNotFoundError:
                continue
            self.path = candidate
            return

    def __enter__(self) -> "EventJournal":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()
