from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from flow.v2.cli import (
    CLEAR_SCREEN,
    ENTER_ALTERNATE_SCREEN,
    HIDE_CURSOR,
    LEAVE_ALTERNATE_SCREEN,
    SHOW_CURSOR,
    main,
)
from flow.v2.processes import RunningFlow, _scratchpad_from_cmdline, print_processes


def test_cli_uses_sysexits_for_usage() -> None:
    assert main([]) == 64
    assert main(["top", "--interval", "0"]) == 64


def test_process_marker_requires_internal_run_and_scratchpad() -> None:
    command = ["python", "-m", "flow.v2.cli", "_run", "--scratchpad", "/tmp/flow-demo-1.md"]
    assert _scratchpad_from_cmdline(command) == "/tmp/flow-demo-1.md"
    assert _scratchpad_from_cmdline(["flow2", "demo.flow"]) is None


def test_ps_json_has_stable_structured_shape() -> None:
    stream = io.StringIO()
    item = RunningFlow(12, 65, "demo", "review", "work_turn", "thread-1", "/tmp/x.md", "/tmp", "now")
    print_processes([item], json_output=True, stream=stream)
    payload = json.loads(stream.getvalue())
    assert payload["flows"][0]["pid"] == 12
    assert payload["flows"][0]["state"] == "review"


def test_catalog_scans_flow_files_without_a_registry(tmp_path: Path, capsys: object) -> None:
    path = tmp_path / "demo.flow"
    path.write_text(
        "flow:\n  name: demo\n  version: 2\ndone:\n  start: true\n  exit: 4\n",
        encoding="utf-8",
    )
    assert main(["catalog", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert payload["flows"][0]["exits"] == {"done": 4}


def test_top_uses_alternate_screen_and_restores_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    class TtyStream(io.StringIO):
        def isatty(self) -> bool:
            return True

    stream = TtyStream()
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr("flow.v2.cli.discover_running_flows", lambda: [])
    monkeypatch.setattr("flow.v2.cli.time.strftime", lambda _format: "2026-08-31 12:00:00")
    monkeypatch.setattr("flow.v2.cli.time.sleep", lambda _interval: (_ for _ in ()).throw(KeyboardInterrupt))

    assert main(["top", "--interval", "0.1"]) == 0

    output = stream.getvalue()
    assert output.startswith(ENTER_ALTERNATE_SCREEN + HIDE_CURSOR + CLEAR_SCREEN)
    assert output.count(ENTER_ALTERNATE_SCREEN) == 1
    assert output.count(CLEAR_SCREEN) == 1
    assert "Flow 2.0 top  2026-08-31 12:00:00" in output
    assert "No running flows." in output
    assert output.endswith(SHOW_CURSOR + LEAVE_ALTERNATE_SCREEN)
