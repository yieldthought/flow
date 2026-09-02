from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from flow.v2.cli import (
    CLEAR_SCREEN,
    CLEAR_LINE,
    ENTER_ALTERNATE_SCREEN,
    HIDE_CURSOR,
    LEAVE_ALTERNATE_SCREEN,
    SHOW_CURSOR,
    _clip_terminal_line,
    _open_chart,
    _redraw_top_frame,
    _wait_for_top_key,
    main,
)
from flow.v2.chart import ChartRenderError
from flow.v2.processes import RunningFlow, _scratchpad_from_cmdline, print_processes
from flow.v2.scratchpad import (
    ScratchpadLock,
    ScratchpadLockedError,
    create_scratchpad,
    journal_path,
    new_metadata,
    read_scratchpad,
)
from flow.v2.spec import load_flow


ANSI_COLOUR = re.compile(r"\x1b\[[0-9;]*m")


class TtyStream(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_cli_uses_sysexits_for_usage() -> None:
    assert main([]) == 64
    assert main(["top", "--interval", "0"]) == 64


def test_resume_state_restarts_completed_checkpoint_with_same_thread_and_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow_path = tmp_path / "demo.flow"
    scratchpad = tmp_path / "flow-demo-1.md"
    flow_path.write_text(
        "flow:\n  name: demo\n  version: 2\nwork:\n  start: true\n  prompt: Work.\n"
        "  transitions:\n    - go: done\ndone:\n  exit: 2\n",
        encoding="utf-8",
    )
    flow = load_flow(flow_path)
    metadata = new_metadata(
        flow_path=str(flow_path),
        flow_digest=flow.digest,
        flow_name=flow.name,
        argv=[str(flow_path)],
        arguments={},
        invocation_cwd=str(tmp_path),
        cwd=str(tmp_path),
        state="done",
        json_output=False,
    )
    metadata.update(
        status="completed",
        phase="completed",
        thread="thread-1",
        exit_code=2,
        resumable=False,
        ended_at="earlier",
        last_error="blocked",
    )
    create_scratchpad(scratchpad, metadata)
    scratchpad.write_text(scratchpad.read_text() + "Durable evidence.\n", encoding="utf-8")
    executed: list[Path] = []
    monkeypatch.setattr("flow.v2.cli._exec_internal", lambda path: executed.append(path) or 0)

    assert main(["resume", str(scratchpad), "--state", "work"]) == 0

    restarted, body = read_scratchpad(scratchpad)
    assert executed == [scratchpad]
    assert restarted["state"] == "work"
    assert restarted["phase"] == "enter_state"
    assert restarted["status"] == "ready"
    assert restarted["thread"] == "thread-1"
    assert restarted["exit_code"] is None
    assert restarted["resumable"] is True
    assert restarted["ended_at"] == ""
    assert restarted["last_error"] == ""
    assert restarted["last_outcome"] == "manually restarted from done at work"
    assert body.endswith("Durable evidence.\n")
    assert not journal_path(scratchpad).exists()


def test_resume_state_refuses_unknown_state_without_changing_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow_path = tmp_path / "demo.flow"
    scratchpad = tmp_path / "flow-demo-1.md"
    flow_path.write_text(
        "flow:\n  name: demo\n  version: 2\ndone:\n  start: true\n  exit: 0\n",
        encoding="utf-8",
    )
    flow = load_flow(flow_path)
    metadata = new_metadata(
        flow_path=str(flow_path),
        flow_digest=flow.digest,
        flow_name=flow.name,
        argv=[str(flow_path)],
        arguments={},
        invocation_cwd=str(tmp_path),
        cwd=str(tmp_path),
        state="done",
        json_output=False,
    )
    metadata.update(status="completed", phase="completed", exit_code=0, resumable=False)
    create_scratchpad(scratchpad, metadata)
    original = scratchpad.read_text(encoding="utf-8")
    monkeypatch.setattr(
        "flow.v2.cli._exec_internal",
        lambda _path: pytest.fail("invalid state was executed"),
    )

    assert main(["resume", str(scratchpad), "--state", "missing"]) == 65
    assert scratchpad.read_text(encoding="utf-8") == original


def test_resume_state_refuses_live_owner_without_changing_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow_path = tmp_path / "demo.flow"
    scratchpad = tmp_path / "flow-demo-1.md"
    flow_path.write_text(
        "flow:\n  name: demo\n  version: 2\nwork:\n  start: true\n  prompt: Work.\n"
        "  transitions:\n    - go: done\ndone:\n  exit: 0\n",
        encoding="utf-8",
    )
    flow = load_flow(flow_path)
    metadata = new_metadata(
        flow_path=str(flow_path),
        flow_digest=flow.digest,
        flow_name=flow.name,
        argv=[str(flow_path)],
        arguments={},
        invocation_cwd=str(tmp_path),
        cwd=str(tmp_path),
        state="work",
        json_output=False,
    )
    metadata.update(status="running", phase="work_turn", thread="thread-1")
    create_scratchpad(scratchpad, metadata)
    original = scratchpad.read_text(encoding="utf-8")
    monkeypatch.setattr(
        "flow.v2.cli._exec_internal",
        lambda _path: pytest.fail("live state was executed"),
    )

    with ScratchpadLock(scratchpad):
        assert main(["resume", str(scratchpad), "--state", "work"]) == 75
        assert scratchpad.read_text(encoding="utf-8") == original


def test_chart_named_output_is_written_without_opening_a_viewer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow_path = tmp_path / "demo.flow"
    output_path = tmp_path / "demo-chart.html"
    flow_path.write_text(
        "flow:\n  name: demo\n  version: 2\ndone:\n  start: true\n  exit: 0\n",
        encoding="utf-8",
    )
    seen: dict[str, object] = {}

    def write(flow: object, output: str, *, theme: str) -> Path:
        seen.update(flow=flow, output=output, theme=theme)
        return output_path

    monkeypatch.setattr("flow.v2.cli.write_chart", write)
    monkeypatch.setattr(
        "flow.v2.cli._open_chart",
        lambda _path: pytest.fail("named chart unexpectedly opened a viewer"),
    )

    assert main(["chart", str(flow_path), "--output", str(output_path), "--theme", "light"]) == 0
    assert seen["output"] == str(output_path)
    assert seen["theme"] == "light"


def test_chart_temporary_output_opens_the_platform_viewer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow_path = tmp_path / "demo.flow"
    chart_path = tmp_path / "temporary-chart.html"
    flow_path.write_text(
        "flow:\n  name: demo\n  version: 2\ndone:\n  start: true\n  exit: 0\n",
        encoding="utf-8",
    )
    opened: list[Path] = []
    monkeypatch.setattr("flow.v2.cli.write_chart", lambda _flow, _output, *, theme: chart_path)
    monkeypatch.setattr("flow.v2.cli._open_chart", lambda path: opened.append(path) or True)

    assert main(["chart", str(flow_path)]) == 0
    assert opened == [chart_path]


def test_open_chart_uses_native_macos_opener(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chart_path = tmp_path / "chart.html"
    seen: list[object] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.extend([command, kwargs])
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("flow.v2.cli.sys.platform", "darwin")
    monkeypatch.setattr("flow.v2.cli.subprocess.run", run)

    assert _open_chart(chart_path)
    assert seen[0] == ["/usr/bin/open", str(chart_path)]
    assert seen[1] == {"capture_output": True, "text": True, "check": False}


def test_open_chart_reports_native_opener_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chart_path = tmp_path / "chart.html"
    monkeypatch.setattr("flow.v2.cli.sys.platform", "darwin")
    monkeypatch.setattr(
        "flow.v2.cli.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 1, "", "no application"),
    )

    with pytest.raises(OSError, match="no application"):
        _open_chart(chart_path)


def test_chart_render_failure_is_a_runtime_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    flow_path = tmp_path / "demo.flow"
    flow_path.write_text(
        "flow:\n  name: demo\n  version: 2\ndone:\n  start: true\n  exit: 0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "flow.v2.cli.write_chart",
        lambda _flow, _output, *, theme: (_ for _ in ()).throw(ChartRenderError("dot failed")),
    )

    assert main(["chart", str(flow_path)]) == 70


def test_chart_named_output_requires_an_html_extension() -> None:
    assert main(["chart", "demo.flow", "--output", "demo.svg"]) == 64


def test_chat_resumes_stopped_thread_while_holding_flow_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow_path = tmp_path / "demo.flow"
    scratchpad = tmp_path / "flow-demo-1.md"
    metadata = new_metadata(
        flow_path=str(flow_path),
        flow_digest="digest",
        flow_name="demo",
        argv=[str(flow_path)],
        arguments={},
        invocation_cwd=str(tmp_path),
        cwd=str(tmp_path),
        state="blocked",
        json_output=False,
    )
    metadata.update(status="completed", phase="completed", thread="thread-1", exit_code=3)
    create_scratchpad(scratchpad, metadata)
    monkeypatch.setattr(sys, "stdin", TtyStream())
    monkeypatch.setattr(sys, "stdout", TtyStream())
    monkeypatch.setattr("flow.v2.cli.shutil.which", lambda _name: "/opt/bin/codex")

    def run(command: list[str], *, cwd: str, check: bool) -> subprocess.CompletedProcess[str]:
        assert command == ["/opt/bin/codex", "resume", "-C", str(tmp_path), "thread-1"]
        assert cwd == str(tmp_path)
        assert check is False
        with pytest.raises(ScratchpadLockedError):
            with ScratchpadLock(scratchpad):
                pass
        scratchpad.write_text("Questions answered during chat.\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("flow.v2.cli.subprocess.run", run)

    assert main(["chat", str(scratchpad)]) == 0
    assert not journal_path(scratchpad).exists()
    repaired, body = read_scratchpad(scratchpad)
    assert repaired["state"] == "blocked"
    assert repaired["phase"] == "completed"
    assert body == "Questions answered during chat.\n"


def test_chat_refuses_a_live_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scratchpad = tmp_path / "flow-demo-1.md"
    metadata = new_metadata(
        flow_path=str(tmp_path / "demo.flow"),
        flow_digest="digest",
        flow_name="demo",
        argv=["demo.flow"],
        arguments={},
        invocation_cwd=str(tmp_path),
        cwd=str(tmp_path),
        state="work",
        json_output=False,
    )
    metadata.update(status="running", phase="work_turn", thread="thread-1")
    create_scratchpad(scratchpad, metadata)
    monkeypatch.setattr(sys, "stdin", TtyStream())
    monkeypatch.setattr(sys, "stdout", TtyStream())
    monkeypatch.setattr(
        "flow.v2.cli.subprocess.run",
        lambda *_args, **_kwargs: pytest.fail("chat launched while Flow owned the scratchpad"),
    )

    with ScratchpadLock(scratchpad):
        assert main(["chat", str(scratchpad)]) == 75


def test_chat_requires_a_thread_and_interactive_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratchpad = tmp_path / "flow-demo-1.md"
    metadata = new_metadata(
        flow_path=str(tmp_path / "demo.flow"),
        flow_digest="digest",
        flow_name="demo",
        argv=["demo.flow"],
        arguments={},
        invocation_cwd=str(tmp_path),
        cwd=str(tmp_path),
        state="work",
        json_output=False,
    )
    create_scratchpad(scratchpad, metadata)

    assert main(["chat", str(scratchpad)]) == 64
    monkeypatch.setattr(sys, "stdin", TtyStream())
    monkeypatch.setattr(sys, "stdout", TtyStream())
    assert main(["chat", str(scratchpad)]) == 65


def test_chat_falls_back_when_recorded_worktree_was_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocation = tmp_path / "invocation"
    invocation.mkdir()
    missing = tmp_path / "removed-worktree"
    scratchpad = tmp_path / "flow-demo-1.md"
    metadata = new_metadata(
        flow_path=str(tmp_path / "demo.flow"),
        flow_digest="digest",
        flow_name="demo",
        argv=["demo.flow"],
        arguments={},
        invocation_cwd=str(invocation),
        cwd=str(missing),
        state="done",
        json_output=False,
    )
    metadata.update(status="completed", phase="completed", thread="thread-2", exit_code=0)
    create_scratchpad(scratchpad, metadata)
    output, errors = TtyStream(), TtyStream()
    monkeypatch.setattr(sys, "stdin", TtyStream())
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(sys, "stderr", errors)
    monkeypatch.setattr("flow.v2.cli.shutil.which", lambda _name: "/opt/bin/codex")
    monkeypatch.setattr(
        "flow.v2.cli.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0),
    )

    assert main(["chat", str(scratchpad)]) == 0
    assert f"original working directory no longer exists: {missing}" in ANSI_COLOUR.sub("", errors.getvalue())
    assert f"using {invocation}" in ANSI_COLOUR.sub("", errors.getvalue())


def test_process_marker_requires_internal_run_and_scratchpad() -> None:
    command = ["python", "-m", "flow.v2.cli", "_run", "--scratchpad", "/tmp/flow-demo-1.md"]
    assert _scratchpad_from_cmdline(command) == "/tmp/flow-demo-1.md"
    assert _scratchpad_from_cmdline(["flow", "demo.flow"]) is None


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
    assert "\x1b[1;38;5;210mflow:" in errors.getvalue()


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
    assert "Flow top  2026-08-31 12:00:00" in visible
    assert "No running flows." in visible
    assert "\x1b[1;38;5;153mFlow top" in output
    assert output.endswith(SHOW_CURSOR + LEAVE_ALTERNATE_SCREEN)


def test_top_redraw_erases_shorter_and_blank_rows() -> None:
    assert _redraw_top_frame("short\n\nfooter\n") == (
        f"{CLEAR_LINE}\rshort\n"
        f"{CLEAR_LINE}\r\n"
        f"{CLEAR_LINE}\rfooter\n"
        "\r"
    )


def test_top_redraw_clips_rows_before_the_terminal_wraps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("flow.v2.cli.shutil.get_terminal_size", lambda _fallback: os.terminal_size((8, 4)))
    rendered = _redraw_top_frame("a much longer row\n")

    assert rendered == f"{CLEAR_LINE}\ra muc...\n\r"
    assert _clip_terminal_line("ab界cd", 5) == "ab..."


def test_watch_replays_json_events_and_returns_the_flow_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    flow_path = tmp_path / "demo.flow"
    flow_path.write_text("flow:\n  name: demo\n  version: 2\ndone:\n  start: true\n  exit: 7\n", encoding="utf-8")
    scratchpad = tmp_path / "flow-demo-1.md"
    state = new_metadata(
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
    state.update(status="running", phase="work_turn")
    create_scratchpad(scratchpad, state)
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)

    with ScratchpadLock(scratchpad) as lock:
        lock.append_event({"event": "state", "elapsed_seconds": 0.1, "state": "done"})
        lock.append_event({"event": "final", "elapsed_seconds": 1.2, "state": "done", "exit_code": 7})
        assert main(["watch", str(scratchpad), "--json"]) == 7

    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [event["event"] for event in events] == ["state", "final"]


def test_watch_checkpoint_fallback_identifies_the_scratchpad(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow_path = tmp_path / "demo.flow"
    scratchpad = tmp_path / "flow-demo-1.md"
    state = new_metadata(
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
    state.update(status="completed", phase="completed", exit_code=5)
    create_scratchpad(scratchpad, state)
    output = io.StringIO()
    ticks = iter([0.0, 2.0])
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr("flow.v2.cli.time.monotonic", lambda: next(ticks))

    assert main(["watch", str(scratchpad), "--json"]) == 5

    event = json.loads(output.getvalue())
    assert event["event"] == "final"
    assert event["scratchpad"] == str(scratchpad)


def test_top_navigates_into_watch_and_back_without_nesting_screens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "flow-alpha-1.md"
    second = tmp_path / "flow-beta-1.md"
    for name, path in (("alpha", first), ("beta", second)):
        state = new_metadata(
            flow_path=str(tmp_path / f"{name}.flow"),
            flow_digest="digest",
            flow_name=name,
            argv=[f"{name}.flow"],
            arguments={},
            invocation_cwd=str(tmp_path),
            cwd=str(tmp_path),
            state="work",
            json_output=False,
        )
        state.update(status="running", phase="work_turn", pid=12)
        create_scratchpad(path, state)
    items = [
        RunningFlow(11, 5, "alpha", "work", "work_turn", "thread-a", str(first), str(tmp_path), "now"),
        RunningFlow(12, 6, "beta", "work", "work_turn", "thread-b", str(second), str(tmp_path), "now"),
    ]
    keys = iter(["\x1b[B", "\x1b[C", "\x1b[D", "q"])
    stream = TtyStream()
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr("flow.v2.cli.discover_running_flows", lambda: items)
    monkeypatch.setattr("flow.v2.cli._wait_for_top_key", lambda _fd, _timeout: next(keys))

    with ScratchpadLock(second) as lock:
        lock.append_event(
            {"event": "start", "elapsed_seconds": 0.1, "flow": "beta", "state": "work", "resumed": False}
        )
        assert main(["top"]) == 0

    output = ANSI_COLOUR.sub("", stream.getvalue())
    assert output.count(ENTER_ALTERNATE_SCREEN) == 1
    assert "Flow watch  beta" in output
    assert "[     0m] start       beta -> work" in output
    assert output.endswith(SHOW_CURSOR + LEAVE_ALTERNATE_SCREEN)


def test_top_key_reader_collects_an_arrow_escape_sequence() -> None:
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"\x1b[B")
        assert _wait_for_top_key(read_fd, 0.1) == "\x1b[B"
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.parametrize("key", ["q", "Q", "\x1b"])
def test_top_quits_on_single_key(key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    stream = TtyStream()
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr("flow.v2.cli.discover_running_flows", lambda: [])
    monkeypatch.setattr("flow.v2.cli._wait_for_top_key", lambda _fd, _timeout: key)

    assert main(["top"]) == 0
    assert stream.getvalue().endswith(SHOW_CURSOR + LEAVE_ALTERNATE_SCREEN)
