"""Filesystem paths used by the runtime."""

from __future__ import annotations

import os
from pathlib import Path


def flow_home() -> Path:
    value = os.environ.get("FLOW_HOME", "~/.flow")
    return Path(value).expanduser().resolve()


def db_path() -> Path:
    return flow_home() / "runtime.sqlite3"


def logs_dir() -> Path:
    return flow_home() / "logs"


def pid_path() -> Path:
    return flow_home() / "daemon.pid"


def socket_path() -> Path:
    return flow_home() / "runtime.sock"


def ensure_home() -> Path:
    root = flow_home()
    root.mkdir(parents=True, exist_ok=True)
    logs_dir().mkdir(parents=True, exist_ok=True)
    return root
