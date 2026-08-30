"""Scratchpad-backed durable state and ephemeral run locking."""

from __future__ import annotations

import os
import re
import socket
import tempfile
import uuid
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil
import yaml

from .constants import METADATA_END, METADATA_START, SCHEMA_VERSION

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

_HEADER_RE = re.compile(
    rf"{re.escape(METADATA_START)}\r?\n(.*?)\r?\n{re.escape(METADATA_END)}\r?\n?",
    re.DOTALL,
)
_SLUG_RE = re.compile(r"[^a-z0-9]+")


class ScratchpadError(ValueError):
    pass


class ScratchpadLockedError(ScratchpadError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def codex_home() -> str:
    return str(Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser().resolve())


def default_scratchpad(flow_path: str | Path, directory: str | Path) -> Path:
    stem = Path(flow_path).stem.lower()
    slug = _SLUG_RE.sub("-", stem).strip("-") or "flow"
    base = Path(directory).expanduser().resolve()
    for run_id in range(1, 1_000_000):
        candidate = base / f"flow-{slug}-{run_id}.md"
        if not candidate.exists():
            return candidate
    raise ScratchpadError(f"could not allocate a scratchpad name in {base}")


def new_metadata(
    *,
    flow_path: str,
    flow_digest: str,
    flow_name: str,
    argv: list[str],
    arguments: dict[str, str],
    invocation_cwd: str,
    cwd: str,
    state: str,
    json_output: bool,
) -> dict[str, Any]:
    now = utc_now()
    return {
        "schema": SCHEMA_VERSION,
        "run_id": str(uuid.uuid4()),
        "flow": flow_name,
        "flow_path": str(Path(flow_path).resolve()),
        "flow_digest": flow_digest,
        "argv": list(argv),
        "arguments": dict(arguments),
        "invocation_cwd": str(Path(invocation_cwd).resolve()),
        "cwd": str(Path(cwd).resolve()),
        "host": socket.gethostname(),
        "codex_home": codex_home(),
        "thread": "",
        "state": state,
        "phase": "enter_state",
        "status": "ready",
        "ready_at": "",
        "pid": None,
        "process_started_at": None,
        "turn_id": "",
        "turn_kind": "",
        "json": bool(json_output),
        "created_at": now,
        "started_at": "",
        "updated_at": now,
        "ended_at": "",
        "last_outcome": "",
        "last_error": "",
        "exit_code": None,
        "resumable": True,
    }


def create_scratchpad(path: str | Path, metadata: dict[str, Any]) -> Path:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    body = f"# {metadata.get('flow', 'Flow')} scratchpad\n\n"
    payload = render_document(metadata, body)
    try:
        with target.open("x", encoding="utf-8") as handle:
            handle.write(payload)
    except FileExistsError as exc:
        raise ScratchpadError(f"scratchpad already exists: {target}") from exc
    return target


def read_scratchpad(path: str | Path) -> tuple[dict[str, Any], str]:
    target = Path(path).expanduser().resolve()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ScratchpadError(f"scratchpad does not exist: {target}") from exc
    match = _HEADER_RE.search(text)
    if match is None:
        raise ScratchpadError(f"scratchpad has no Flow metadata header: {target}")
    try:
        metadata = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise ScratchpadError(f"invalid Flow metadata in {target}: {exc}") from exc
    if not isinstance(metadata, dict):
        raise ScratchpadError(f"Flow metadata must be a mapping: {target}")
    validate_metadata(metadata, target)
    body = text[: match.start()] + text[match.end() :]
    return metadata, body.lstrip("\r\n")


def repair_scratchpad(path: str | Path, metadata: dict[str, Any]) -> None:
    target = Path(path).expanduser().resolve()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    match = _HEADER_RE.search(text)
    if match is None:
        body = text.lstrip("\r\n")
    else:
        body = (text[: match.start()] + text[match.end() :]).lstrip("\r\n")
    metadata["updated_at"] = utc_now()
    _atomic_write(target, render_document(metadata, body))


def render_document(metadata: dict[str, Any], body: str) -> str:
    header = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=False).rstrip()
    suffix = body if not body or body.endswith("\n") else body + "\n"
    return f"{METADATA_START}\n{header}\n{METADATA_END}\n\n{suffix}"


def validate_metadata(metadata: dict[str, Any], path: Path | None = None) -> None:
    location = f" in {path}" if path else ""
    if metadata.get("schema") != SCHEMA_VERSION:
        raise ScratchpadError(f"unsupported scratchpad schema{location}: {metadata.get('schema')!r}")
    required = ("run_id", "flow_path", "flow_digest", "cwd", "host", "codex_home", "state", "phase")
    missing = [name for name in required if not metadata.get(name)]
    if missing:
        raise ScratchpadError(f"scratchpad metadata is missing {', '.join(missing)}{location}")


def mark_process(metadata: dict[str, Any]) -> None:
    proc = psutil.Process()
    metadata["pid"] = proc.pid
    metadata["process_started_at"] = proc.create_time()
    metadata["status"] = "running"
    metadata["started_at"] = metadata.get("started_at") or utc_now()
    metadata["ended_at"] = ""


def same_process(metadata: dict[str, Any], process: psutil.Process) -> bool:
    try:
        return int(metadata.get("pid") or 0) == process.pid and abs(
            float(metadata.get("process_started_at") or 0.0) - process.create_time()
        ) < 0.01
    except (psutil.Error, TypeError, ValueError):
        return False


class ScratchpadLock(AbstractContextManager["ScratchpadLock"]):
    def __init__(self, scratchpad: str | Path) -> None:
        self.path = Path(f"{Path(scratchpad).expanduser().resolve()}.lock")
        self.handle: Any = None

    def __enter__(self) -> "ScratchpadLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            if fcntl is not None:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:  # pragma: no cover - Windows
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
        except (BlockingIOError, OSError) as exc:
            self.handle.close()
            self.handle = None
            raise ScratchpadLockedError(f"flow is already running: {self.path.with_suffix('')}") from exc
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(f"{os.getpid()}\n")
        self.handle.flush()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        if self.handle is None:
            return
        if fcntl is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
