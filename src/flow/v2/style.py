"""Stream-aware functional pastel styling for Flow 2.0."""

from __future__ import annotations

import argparse
import inspect
import sys
from typing import Any, TextIO

from flow.ansi import PALETTE, color
from flow.common import ansi_enabled


_ARGPARSE_HAS_COLOUR = "color" in inspect.signature(argparse.ArgumentParser).parameters


class StyledArgumentParser(argparse.ArgumentParser):
    """Argparse parser whose help follows Flow's stream-aware colour policy."""

    def __init__(self, *args: Any, colour: bool = True, **kwargs: Any) -> None:
        self._flow_colour = colour
        if _ARGPARSE_HAS_COLOUR:
            kwargs["color"] = False
        super().__init__(*args, **kwargs)

    def _print_message(self, message: str, file: TextIO | None = None) -> None:
        stream = file or sys.stdout
        super()._print_message(style_help(message, stream=stream, enabled=self._flow_colour), stream)


def colour_enabled(stream: TextIO) -> bool:
    return bool(getattr(stream, "isatty", lambda: False)()) and ansi_enabled()


def paint(
    text: str,
    code: int,
    *,
    stream: TextIO | None = None,
    enabled: bool | None = None,
    bold: bool = False,
) -> str:
    active = colour_enabled(stream) if enabled is None and stream is not None else bool(enabled)
    return color(text, code, bold=bold) if active else text


def style_help(message: str, *, stream: TextIO, enabled: bool = True) -> str:
    if not enabled or not colour_enabled(stream):
        return message

    rendered: list[str] = []
    for line in message.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        ending = line[len(content) :]
        if content.startswith("usage:"):
            prefix, separator, detail = content.partition(" ")
            content = paint(prefix, PALETTE.bright, enabled=True, bold=True)
            if separator:
                content += separator + paint(detail, PALETTE.subtle, enabled=True)
        elif content and not content[0].isspace() and content.endswith(":"):
            content = paint(content, PALETTE.bright, enabled=True, bold=True)
        elif content.lstrip().startswith("-"):
            indent = content[: len(content) - len(content.lstrip())]
            option, separator, detail = content.lstrip().partition("  ")
            content = indent + paint(option, PALETTE.accent, enabled=True, bold=True)
            if separator:
                content += separator + detail
        rendered.append(content + ending)
    return "".join(rendered)


def phase_colour(phase: str) -> int:
    if "wait" in phase:
        return PALETTE.warn
    if phase in {"work_pending", "work_turn", "continue_pending", "continue_turn"}:
        return PALETTE.info
    if phase in {"evaluate", "decision_pending", "decision_turn"}:
        return PALETTE.accent
    if phase == "enter_state":
        return PALETTE.state
    if "interrupt" in phase or "error" in phase:
        return PALETTE.error
    return PALETTE.muted


def status_colour(status: str) -> int:
    if status == "completed":
        return PALETTE.ok
    if status in {"needs-help", "error"}:
        return PALETTE.error
    if status == "interrupted":
        return PALETTE.warn
    if status == "running":
        return PALETTE.info
    return PALETTE.muted
