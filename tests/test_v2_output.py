from __future__ import annotations

import io
import os
import re
import time
from datetime import datetime

import pytest

from flow.v2.output import HUMAN_EVENT_KINDS, HUMAN_LABEL_WIDTH, Reporter, _human_duration, elapsed_stamp


ANSI_COLOUR = re.compile(r"\x1b\[[0-9;]*m")


class TtyStream(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_human_event_labels_share_one_visible_column(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    reporter = Reporter(stream=TtyStream())
    prefix = elapsed_stamp(0) + " "
    detail_column = len(prefix) + HUMAN_LABEL_WIDTH + 1
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
        assert visible[len(prefix) : detail_column].rstrip() == kind.replace("_", " ")
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

    Reporter(stream=TtyStream(), error_stream=error_stream).diagnostic("flow resume scratchpad.md")

    assert "\x1b[38;5;151mflow resume scratchpad.md" in error_stream.getvalue()


def test_reporter_sends_the_same_structured_event_to_the_journal() -> None:
    journal: list[dict[str, object]] = []
    output = io.StringIO()
    reporter = Reporter(stream=output, event_sink=journal.append)

    emitted = reporter.emit("state", state="review")

    assert journal == [emitted]
    assert journal[0]["event"] == "state"
    assert "review" in output.getvalue()


def test_resumed_start_is_explicit_about_checkpoint_and_thread() -> None:
    reporter = Reporter(stream=io.StringIO())

    line = reporter._human_line(
        "start",
        {
            "flow": "agi-watcher",
            "state": "check-news",
            "phase": "transition_wait",
            "ready_at": "2026-08-31T12:45:29Z",
            "timestamp": "2026-08-31T12:15:29Z",
            "thread": "01a057a3-1f6f-7ac3-8680-938f412d360f",
            "resumed": True,
        },
    )

    local_ready = datetime.fromisoformat("2026-08-31T12:45:29+00:00").astimezone()
    assert line == (
        "[     0m] resume      agi-watcher at check-news "
        f"(waiting until {local_ready:%H:%M} on {local_ready:%b} {local_ready.day} (30 mins); thread 01a057a3)"
    )


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="requires POSIX timezone switching")
def test_waiting_event_uses_local_time_and_human_duration() -> None:
    previous_timezone = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "Europe/Berlin"
        time.tzset()
        reporter = Reporter(stream=io.StringIO())

        line = reporter._human_line(
            "waiting",
            {
                "state": "check-baseline-ci",
                "timestamp": "2026-09-01T13:13:47Z",
                "ready_at": "2026-09-01T14:13:46Z",
            },
        )
    finally:
        if previous_timezone is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_timezone
        time.tzset()

    assert line == "[     0m] waiting     check-baseline-ci until 16:13 on Sep 1 (59 mins)"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (5 * 60 + 59, "[     5m]"),
        (3 * 3600 + 14 * 60, "[ 3h 14m]"),
        (10 * 3600, "[10h  0m]"),
        (24 * 3600 + 17 * 60, "[1d  0h 17m]"),
        (123 * 86400 + 4 * 3600 + 2 * 60, "[123d  4h  2m]"),
    ],
)
def test_elapsed_stamp_omits_seconds_and_leading_zero_units(seconds: float, expected: str) -> None:
    assert elapsed_stamp(seconds) == expected


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (30, "30s"),
        (60, "1 min"),
        (59 * 60, "59 mins"),
        (2 * 3600 + 30 * 60, "2h 30m"),
        (2 * 86400 + 8 * 3600, "2d 8h"),
    ],
)
def test_human_wait_duration(seconds: float, expected: str) -> None:
    assert _human_duration(seconds) == expected
