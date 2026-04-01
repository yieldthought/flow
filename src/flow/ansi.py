"""Small pastel 256-color helpers for CLI output."""

from __future__ import annotations

from dataclasses import dataclass

from .common import ansi_enabled


@dataclass(frozen=True)
class Palette:
    bright: int = 153
    muted: int = 110
    info: int = 117
    state: int = 189
    ok: int = 114
    warn: int = 222
    error: int = 210
    subtle: int = 248
    dim: int = 242
    accent: int = 151


PALETTE = Palette()


def color(text: str, code: int, *, bold: bool = False) -> str:
    if not ansi_enabled():
        return text
    prefix = "1;" if bold else ""
    return f"\x1b[{prefix}38;5;{code}m{text}\x1b[0m"


def bold(text: str) -> str:
    if not ansi_enabled():
        return text
    return f"\x1b[1m{text}\x1b[0m"
