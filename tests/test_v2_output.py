from __future__ import annotations

import io
import re

import pytest

from flow.v2.output import HUMAN_EVENT_KINDS, HUMAN_LABEL_WIDTH, Reporter


ANSI_COLOUR = re.compile(r"\x1b\[[0-9;]*m")


class TtyStream(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_human_event_labels_share_one_visible_column(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    reporter = Reporter(stream=TtyStream())
    detail_column = len("[00:00] ") + HUMAN_LABEL_WIDTH + 1
    fields = {
        "elapsed_seconds": 1.0,
        "exit_code": 0,
        "flow": "flow",
        "from_state": "from",
        "message": "message",
        "ready_at": "later",
        "reason": "reason",
        "signal": "SIGINT",
        "state": "detail",
        "summary": "summary",
        "to_state": "to",
    }

    for kind in HUMAN_EVENT_KINDS:
        line = reporter._human_line(kind, fields)
        assert "\x1b[" in line
        visible = ANSI_COLOUR.sub("", line)
        assert visible[len("[00:00] ") : detail_column].rstrip() == kind.replace("_", " ")
        assert visible[detail_column:] == reporter._detail(kind, fields)

    assert HUMAN_LABEL_WIDTH == len("interrupted")


def test_reporter_never_colours_redirected_or_json_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)

    redirected = io.StringIO()
    Reporter(stream=redirected).emit("state", state="review")
    assert "\x1b[" not in redirected.getvalue()

    json_stream = TtyStream()
    Reporter(stream=json_stream, json_output=True).emit("state", state="review")
    assert "\x1b[" not in json_stream.getvalue()


def test_reporter_diagnostics_follow_stderr_colour_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    error_stream = TtyStream()

    Reporter(stream=TtyStream(), error_stream=error_stream).diagnostic("flow2 resume scratchpad.md")

    assert "\x1b[38;5;151mflow2 resume scratchpad.md" in error_stream.getvalue()


def test_resumed_start_is_explicit_about_checkpoint_and_thread() -> None:
    reporter = Reporter(stream=io.StringIO())

    line = reporter._human_line(
        "start",
        {
            "flow": "agi-watcher",
            "state": "check-news",
            "phase": "transition_wait",
            "ready_at": "2026-08-31T12:45:29Z",
            "thread": "01a057a3-1f6f-7ac3-8680-938f412d360f",
            "resumed": True,
        },
    )

    assert line == (
        "[00:00] resume      agi-watcher at check-news "
        "(waiting until 2026-08-31T12:45:29Z; thread 01a057a3)"
    )
