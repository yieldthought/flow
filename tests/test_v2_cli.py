from __future__ import annotations

import io
import json
import re
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
from flow.v2.scratchpad import create_scratchpad, new_metadata


ANSI_COLOUR = re.compile(r"\x1b\[[0-9;]*m")


class TtyStream(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_cli_uses_sysexits_for_usage() -> None:
    assert main([]) == 64
    assert main(["top", "--interval", "0"]) == 64


def test_process_marker_requires_internal_run_and_scratchpad() -> None:
    command = ["python", "-m", "flow.v2.cli", "_run", "--scratchpad", "/tmp/flow-demo-1.md"]
    assert _scratchpad_from_cmdline(command) == "/tmp/flow-demo-1.md"
    assert _scratchpad_from_cmdline(["flow2", "demo.flow"]) is None


def test_ps_json_has_stable_structured_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    stream = TtyStream()
    item = RunningFlow(12, 65, "demo", "review", "work_turn", "thread-1", "/tmp/x.md", "/tmp", "now")
    print_processes([item], json_output=True, stream=stream, colour=True)
    assert "\x1b[" not in stream.getvalue()
    payload = json.loads(stream.getvalue())
    assert payload["flows"][0]["pid"] == 12
    assert payload["flows"][0]["state"] == "review"


def test_process_table_uses_functional_pastel_colours(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    item = RunningFlow(
        12,
        65,
        "demo",
        "review",
        "transition_wait",
        "thread-1",
        "/tmp/x.md",
        "/tmp",
        "now",
    )
    plain = io.StringIO()
    coloured = io.StringIO()

    print_processes([item], json_output=False, stream=plain)
    print_processes([item], json_output=False, stream=coloured, colour=True)

    output = coloured.getvalue()
    assert ANSI_COLOUR.sub("", output) == plain.getvalue()
    assert "\x1b[1;38;5;153mdemo" in output
    assert "\x1b[1;38;5;189mreview" in output
    assert "\x1b[38;5;222mtransition_wait" in output


def test_catalog_scans_flow_files_without_a_registry(tmp_path: Path, capsys: object) -> None:
    path = tmp_path / "demo.flow"
    path.write_text(
        "flow:\n  name: demo\n  version: 2\ndone:\n  start: true\n  exit: 4\n",
        encoding="utf-8",
    )
    assert main(["catalog", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert payload["flows"][0]["exits"] == {"done": 4}


def test_cli_colours_human_terminal_surfaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    flow_path = tmp_path / "demo.flow"
    flow_path.write_text(
        "flow:\n  name: demo\n  version: 2\ndone:\n  start: true\n  exit: 4\n",
        encoding="utf-8",
    )
    scratchpad = tmp_path / "flow-demo-1.md"
    metadata = new_metadata(
        flow_path=str(flow_path),
        flow_digest="digest",
        flow_name="demo",
        argv=[str(flow_path)],
        arguments={},
        invocation_cwd=str(tmp_path),
        cwd=str(tmp_path),
        state="done",
        json_output=False,
    )
    metadata.update(phase="completed", status="completed", exit_code=4, thread="thread-1")
    create_scratchpad(scratchpad, metadata)
    output = TtyStream()
    errors = TtyStream()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(sys, "stderr", errors)
    monkeypatch.setattr(
        "flow.v2.cli.discover_running_flows",
        lambda: [
            RunningFlow(12, 65, "demo", "done", "work_turn", "thread-1", str(scratchpad), "/tmp", "now")
        ],
    )

    assert main([]) == 64
    assert main(["inspect", str(scratchpad)]) == 0
    assert main(["validate", str(flow_path)]) == 0
    assert main(["catalog", str(tmp_path)]) == 0
    assert main(["ps"]) == 0
    assert main(["missing.flow"]) == 65

    human_output = output.getvalue()
    assert "\x1b[1;38;5;153musage:" in human_output
    assert "\x1b[1;38;5;153mdemo" in human_output
    assert "\x1b[1;38;5;189mdone" in human_output
    assert "\x1b[1;38;5;114mvalid" in human_output
    assert "\x1b[1;38;5;210m4" in human_output
    assert "\x1b[1;38;5;210mflow2:" in errors.getvalue()


def test_cli_keeps_redirected_and_json_output_plain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    path = tmp_path / "demo.flow"
    path.write_text(
        "flow:\n  name: demo\n  version: 2\ndone:\n  start: true\n  exit: 0\n",
        encoding="utf-8",
    )

    redirected = io.StringIO()
    monkeypatch.setattr(sys, "stdout", redirected)
    assert main(["validate", str(path)]) == 0
    assert "\x1b[" not in redirected.getvalue()

    json_output = TtyStream()
    monkeypatch.setattr(sys, "stdout", json_output)
    assert main(["catalog", str(tmp_path), "--json"]) == 0
    assert "\x1b[" not in json_output.getvalue()
    assert json.loads(json_output.getvalue())["flows"][0]["name"] == "demo"


def test_no_color_disables_terminal_styling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("NO_COLOR", "1")
    stream = TtyStream()
    monkeypatch.setattr(sys, "stdout", stream)

    assert main([]) == 64
    assert "\x1b[" not in stream.getvalue()


def test_command_help_is_coloured_except_in_json_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    stream = TtyStream()
    monkeypatch.setattr(sys, "stdout", stream)

    with pytest.raises(SystemExit, match="0"):
        main(["ps", "--help"])
    assert "\x1b[1;38;5;153musage:" in stream.getvalue()
    assert "\x1b[1;38;5;151m-h, --help" in stream.getvalue()

    stream.seek(0)
    stream.truncate(0)
    with pytest.raises(SystemExit, match="0"):
        main(["ps", "--json", "--help"])
    assert "\x1b[" not in stream.getvalue()


def test_top_uses_alternate_screen_and_restores_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
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
    visible = ANSI_COLOUR.sub("", output)
    assert "Flow 2.0 top  2026-08-31 12:00:00" in visible
    assert "No running flows." in visible
    assert "\x1b[1;38;5;153mFlow 2.0 top" in output
    assert output.endswith(SHOW_CURSOR + LEAVE_ALTERNATE_SCREEN)


@pytest.mark.parametrize("key", ["q", "Q", "\x1b"])
def test_top_quits_on_single_key(key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    stream = TtyStream()
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr("flow.v2.cli.discover_running_flows", lambda: [])
    monkeypatch.setattr("flow.v2.cli._wait_for_top_key", lambda _fd, _timeout: key)

    assert main(["top"]) == 0
    assert stream.getvalue().endswith(SHOW_CURSOR + LEAVE_ALTERNATE_SCREEN)
