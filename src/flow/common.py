"""Shared constants and small helpers."""

from __future__ import annotations

import json
import os
import re
import getpass
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RESERVED_STATE_NAMES = {"stopped", "needs_help", "interaction"}
IMPLICIT_TRANSITION_KEEP_WORKING = "keep_working"
IMPLICIT_TRANSITION_NEEDS_HELP = "needs_help"
DEFAULT_MODE = "yolo"
DEFAULT_THINKING = "xhigh"
VALID_THINKING = {"low", "medium", "high", "xhigh"}
VALID_MODES = {"yolo", "full-auto", "workspace-write", "read-only", "danger-full-access"}
PLACEHOLDER_RE = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")
WAIT_PART_RE = re.compile(r"(\d+)([smhd])", re.IGNORECASE)
PHASE_ALIASES = {
    "delayed": "waiting",
    "waiting_turn": "working",
    "terminal": "finished",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_utc(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
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
    return parsed.astimezone(timezone.utc)


def expand_path(value: str) -> str:
    return str(Path(value).expanduser().resolve())


def canonical_cli_name(name: str) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name).replace("_", "-")
    return re.sub(r"-{2,}", "-", text).strip("-").lower()


def normalize_phase(value: str | None) -> str:
    text = str(value or "")
    return PHASE_ALIASES.get(text, text)


def to_json(data: Any) -> str:
    return json.dumps(_json_ready(data), indent=2, sort_keys=True)


def _json_ready(data: Any) -> Any:
    if is_dataclass(data):
        return {key: _json_ready(value) for key, value in asdict(data).items()}
    if isinstance(data, dict):
        return {str(key): _json_ready(value) for key, value in data.items()}
    if isinstance(data, (list, tuple)):
        return [_json_ready(value) for value in data]
    return data


def duration_seconds(started_at: str | None, ended_at: str | None = None) -> float:
    start = parse_utc(started_at)
    end = parse_utc(ended_at) if ended_at else utc_now()
    if start is None or end is None:
        return 0.0
    return max(0.0, (end - start).total_seconds())


def ansi_enabled() -> bool:
    term = os.environ.get("TERM", "")
    if os.environ.get("NO_COLOR"):
        return False
    return bool(term) and term != "dumb"


def current_actor() -> str:
    for key in ("FLOW_ACTOR", "USER", "LOGNAME"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    try:
        return getpass.getuser().strip() or "unknown"
    except Exception:
        return "unknown"


def parse_wait_seconds(value: str) -> int:
    text = (value or "").strip().lower()
    if not text:
        raise ValueError("wait must be a non-empty duration like '10m'")

    total = 0
    position = 0
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    while position < len(text):
        while position < len(text) and text[position].isspace():
            position += 1
        if position >= len(text):
            break
        match = WAIT_PART_RE.match(text, position)
        if match is None:
            raise ValueError(f"invalid wait value '{value}'")
        amount = int(match.group(1))
        unit = match.group(2).lower()
        total += amount * multipliers[unit]
        position = match.end()
    return total
