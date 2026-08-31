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
        visible = ANSI_COLOUR.sub("", line)
        assert visible[len("[00:00] ") : detail_column].rstrip() == kind.replace("_", " ")
        assert visible[detail_column:] == reporter._detail(kind, fields)

    assert HUMAN_LABEL_WIDTH == len("interrupted")
