"""Scratchpad helpers."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .paths import agent_scratchpad_dir, agent_scratchpad_path


def scratchpad_path(agent_id: int) -> Path:
    return agent_scratchpad_path(agent_id)


def scratchpad_path_text(agent: dict[str, Any]) -> str:
    return str(scratchpad_path(_agent_id(agent)))


def read_scratchpad_text(agent: dict[str, Any]) -> str:
    path = scratchpad_path(_agent_id(agent))
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def ensure_scratchpad_dir(agent: dict[str, Any]) -> str:
    path = agent_scratchpad_dir(_agent_id(agent))
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def remove_scratchpad_dir(agent_id: int) -> None:
    try:
        shutil.rmtree(agent_scratchpad_dir(agent_id))
    except FileNotFoundError:
        return


def _agent_id(agent: dict[str, Any]) -> int:
    try:
        return int(agent["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("scratchpad helpers require an agent with a valid integer 'id'") from exc
