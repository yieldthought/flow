from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from flow.common import parse_utc
from flow.backend import (
    CodexBackend,
    TurnObservation,
    _attach_env,
    _codex_launch_failure_reason,
    _find_turn,
    _format_agent_args,
    _is_codex_process_name,
    _looks_like_codex_prompt_ready,
    _looks_like_codex_trust_prompt,
    _looks_like_codex_tui_ready,
    _resolve_codex_launcher,
    _session_env_passthrough,
    _session_env_unset_names,
    _thread_name_updated_seen,
    _visible_prompt_content,
)


def test_session_env_unset_names_preserves_codex_home(monkeypatch: object) -> None:
    monkeypatch.setenv("CODEX_HOME", "/tmp/shared-codex-home")
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")
    monkeypatch.setenv("CODEX_CI", "1")
    monkeypatch.setenv("CHATGPT_DESKTOP_THREAD_ID", "desktop-thread")
    monkeypatch.setenv("__CFBundleIdentifier", "com.openai.codex")
    monkeypatch.setenv("OTHER_ENV", "keep-me")

    names = _session_env_unset_names()

    assert "CODEX_HOME" not in names
    assert "CODEX_THREAD_ID" in names
    assert "CODEX_CI" in names
    assert "CHATGPT_DESKTOP_THREAD_ID" in names
    assert "__CFBundleIdentifier" in names


def test_session_env_passthrough_includes_runtime_specific_homes(monkeypatch: object) -> None:
    monkeypatch.setenv("FLOW_HOME", "/tmp/flow-home")
    monkeypatch.setenv("CODEX_HOME", "/tmp/codex-home")
    monkeypatch.setenv("HOME", "/tmp/home")
    monkeypatch.setenv("PATH", "/tmp/bin:/usr/bin")
    monkeypatch.setenv("VIRTUAL_ENV", "/tmp/venv")

    values = _session_env_passthrough()

    assert values == {
        "FLOW_HOME": "/tmp/flow-home",
        "CODEX_HOME": "/tmp/codex-home",
        "HOME": "/tmp/home",
        "PATH": "/tmp/bin:/usr/bin",
        "VIRTUAL_ENV": "/tmp/venv",
    }


def test_launch_command_disables_app_server_tui() -> None:
    backend = CodexBackend()
    command = backend._launch_command({"cwd": "/tmp/work", "mode": "yolo", "thinking": "xhigh"})

    assert "--disable tui_app_server" in command
    assert "-c trust_level=trusted" in command
    assert "-c features.fast_mode=false" in command


def test_launch_command_can_enable_fast_mode() -> None:
    backend = CodexBackend()

    command = backend._launch_command({"cwd": "/tmp/work", "mode": "yolo", "thinking": "xhigh", "fast": 1})

    assert "-c service_tier=fast" in command
    assert "-c features.fast_mode=true" in command


def test_launch_command_uses_resolved_codex_launcher(monkeypatch: object) -> None:
    backend = CodexBackend()
    monkeypatch.setattr(
        "flow.backend._resolve_codex_launcher",
        lambda: ("/home/moconnor/.nvm/versions/node/v20.20.0/bin/codex", "/home/moconnor/.nvm/versions/node/v20.20.0/bin"),
    )

    command = backend._launch_command({"cwd": "/tmp/work", "mode": "yolo", "thinking": "xhigh"})

    assert command.startswith(
        "env PATH=/home/moconnor/.nvm/versions/node/v20.20.0/bin:$PATH /home/moconnor/.nvm/versions/node/v20.20.0/bin/codex"
    )


def test_resolve_codex_launcher_falls_back_to_real_home_nvm(monkeypatch: object, tmp_path: Path) -> None:
    real_home = tmp_path / "real-home"
    node_bin = real_home / ".nvm" / "versions" / "node" / "v22.22.1" / "bin"
    node_bin.mkdir(parents=True)
    (node_bin / "codex").write_text("", encoding="utf-8")
    (node_bin / "node").write_text("", encoding="utf-8")
    (node_bin / "codex").chmod(0o755)
    (node_bin / "node").chmod(0o755)

    monkeypatch.setattr("flow.backend.shutil.which", lambda name: None)
    monkeypatch.setattr("flow.backend._real_user_home", lambda: real_home)

    codex_executable, codex_bin_dir = _resolve_codex_launcher()

    assert codex_executable == str(node_bin / "codex")
    assert codex_bin_dir == str(node_bin)


def test_new_session_command_carries_runtime_specific_env(monkeypatch: object) -> None:
    monkeypatch.setenv("FLOW_HOME", "/tmp/flow-home")
    monkeypatch.setenv("CODEX_HOME", "/tmp/codex-home")
    monkeypatch.setenv("HOME", "/tmp/home")
    monkeypatch.setenv("PATH", "/tmp/bin:/usr/bin")
    backend = CodexBackend()

    command = backend._new_session_command("flow-agent-1", "/tmp/work", "/bin/bash")

    assert "FLOW_HOME=/tmp/flow-home" in command
    assert "CODEX_HOME=/tmp/codex-home" in command
    assert "HOME=/tmp/home" in command
    assert "PATH=/tmp/bin:/usr/bin" in command


def test_workspace_write_launch_command_adds_scratchpad_dir(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    backend = CodexBackend()

    command = backend._launch_command({"id": 7, "cwd": "/tmp/work", "mode": "workspace-write", "thinking": "xhigh"})

    assert "-s workspace-write" in command
    assert f"--add-dir {tmp_path / '.flow' / 'scratchpads' / 'agent-7'}" in command


def test_launch_command_adds_child_scratchpad_dirs_from_pending_state(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setenv("FLOW_HOME", str(tmp_path / ".flow"))
    backend = CodexBackend()

    command = backend._launch_command(
        {
            "id": 7,
            "cwd": "/tmp/work",
            "mode": "workspace-write",
            "thinking": "xhigh",
            "pending_state_json": '{"kind":"children_wake","add_dirs":["/tmp/child-17","/tmp/child-18","/tmp/child-17"]}',
        }
    )

    assert "--add-dir /tmp/child-17" in command
    assert "--add-dir /tmp/child-18" in command
    assert command.count("--add-dir /tmp/child-17") == 1


def test_codex_tui_ready_probe_accepts_standalone_banner() -> None:
    assert _looks_like_codex_tui_ready(
        """
╭────────────────────────────────────────────────────╮
│ >_ OpenAI Codex (v0.117.0)                         │
│                                                    │
│ model:     gpt-5.4 xhigh   fast   /model to change │
│ directory: ~/work/agent-flows                      │
╰────────────────────────────────────────────────────╯
""".strip()
    )


def test_codex_tui_ready_probe_accepts_active_conversation_view() -> None:
    assert _looks_like_codex_tui_ready(
        """
› [flow-control]
  agent_id: 1
  marker: flow-agent-1-abc123
  kind: transition_eval
  [/flow-control]

• {"choice":"check-run","reason":"still queued"}

› Run /review on my current changes

  gpt-5.4 medium fast · 5h 96% · weekly 95%
""".strip(),
        current_command="codex-aarch64-a",
    )


def test_codex_prompt_ready_probe_accepts_idle_conversation_view() -> None:
    assert _looks_like_codex_prompt_ready(
        """
• {"choice":"notify-pass","reason":"The workflow run completed successfully."}

› Summarize recent commits

  gpt-5.4 low fast · 5h 96% · weekly 95%
""".strip(),
        current_command="codex-aarch64-a",
    )


def test_codex_prompt_ready_probe_accepts_node_hosted_codex() -> None:
    assert _looks_like_codex_prompt_ready(
        """
╭─────────────────────────────────────────────╮
│ >_ OpenAI Codex (v0.114.0)                  │
│                                             │
│ model:     gpt-5.4 xhigh   /model to change │
│ directory: /localdev/moconnor/mport         │
╰─────────────────────────────────────────────╯

› Find and fix a bug in @filename

  gpt-5.4 xhigh · 100% left · /localdev/moconnor/mport
""".strip(),
        current_command="node",
    )


def test_codex_prompt_ready_probe_accepts_wrapped_prompt_above_status_and_blank_tail() -> None:
    assert _looks_like_codex_prompt_ready(
        """
╭─────────────────────────────────────────────╮
│ >_ OpenAI Codex (v0.121.0)                  │
│                                             │
│ model:     gpt-5.4 xhigh   /model to change │
│ directory: /localdev/moconnor/mport         │
╰─────────────────────────────────────────────╯


› codex --disable tui_app_server --no-alt-screen --cd /localdev/moconnor/mport
  --dangerously-bypass-approvals-and-sandbox -c trust_level=trusted -c
  model_reasoning_effort=xhigh -c check_for_update_on_startup=false


  gpt-5.4 xhigh · /localdev/moconnor/mport












""".strip(),
        current_command="node",
    )


def test_codex_prompt_ready_probe_rejects_startup_banner_without_prompt() -> None:
    assert not _looks_like_codex_prompt_ready(
        """
╭─────────────────────────────────────────────╮
│ >_ OpenAI Codex (v0.121.0)                  │
│                                             │
│ model:     gpt-5.4 xhigh   /model to change │
│ directory: /localdev/moconnor/mport         │
╰─────────────────────────────────────────────╯
""".strip(),
        current_command="node",
    )


def test_codex_prompt_ready_probe_rejects_status_line_without_visible_prompt_text() -> None:
    assert not _looks_like_codex_prompt_ready(
        """
• Working complete

  gpt-5.4 xhigh · /localdev/moconnor/mport
""".strip(),
        current_command="node",
    )


def test_codex_prompt_ready_probe_rejects_trust_prompt() -> None:
    assert not _looks_like_codex_prompt_ready(
        """
> You are in /tmp/agent-flows

  Do you trust the contents of this directory? Working with untrusted contents
  comes with higher risk of prompt injection.

› 1. Yes, continue
  2. No, quit
""".strip(),
        current_command="codex-aarch64-a",
    )


def test_codex_prompt_ready_probe_rejects_previous_thread_name_hint() -> None:
    assert not _looks_like_codex_prompt_ready(
        """
› [flow 1] demo ticket=123

  gpt-5.4 low fast · 5h 96% · weekly 95%
""".strip(),
        current_command="codex-aarch64-a",
    )


def test_codex_prompt_ready_probe_rejects_previous_rename_command() -> None:
    assert not _looks_like_codex_prompt_ready(
        """
› /rename [flow 1] demo

  gpt-5.4 low fast · 5h 96% · weekly 95%
""".strip(),
        current_command="codex-aarch64-a",
    )


def test_codex_trust_prompt_probe_detects_workspace_confirmation_screen() -> None:
    assert _looks_like_codex_trust_prompt(
        """
> You are in /tmp/agent-flows

  Do you trust the contents of this directory? Working with untrusted contents
  comes with higher risk of prompt injection.

› 1. Yes, continue
  2. No, quit

  Press enter to continue
""".strip(),
        current_command="codex-aarch64-a",
    )


def test_codex_trust_prompt_probe_ignores_stale_scrollback_prompt() -> None:
    filler = "\n".join(f"  filler line {index}" for index in range(24))
    text = f"""
> You are in /tmp/agent-flows

  Do you trust the contents of this directory? Working with untrusted contents
  comes with higher risk of prompt injection.

› 1. Yes, continue
  2. No, quit

  Press enter to continue


{filler}

⚠ MCP startup incomplete (failed: codex_apps)

› Implement {{feature}}

  gpt-5.4 low · /tmp/agent-flows
""".strip()
    assert not _looks_like_codex_trust_prompt(text, current_command="node")


def test_codex_prompt_ready_probe_accepts_prompt_after_trust_prompt_scrolls_off() -> None:
    filler = "\n".join(f"  filler line {index}" for index in range(24))
    text = f"""
> You are in /tmp/agent-flows

  Do you trust the contents of this directory? Working with untrusted contents
  comes with higher risk of prompt injection.

› 1. Yes, continue
  2. No, quit

  Press enter to continue


{filler}

⚠ MCP startup incomplete (failed: codex_apps)

› Implement {{feature}}

  gpt-5.4 low · /tmp/agent-flows
""".strip()
    assert _looks_like_codex_prompt_ready(text, current_command="node")


def test_launch_signature_ignores_thread_resume_suffix() -> None:
    backend = CodexBackend()
    agent = {"cwd": "/tmp/work", "mode": "yolo", "thinking": "xhigh", "thread_id": "thread-123"}

    assert "resume thread-123" in backend._launch_command(agent)
    assert "resume" not in backend._launch_signature(agent)


def test_send_prompt_waits_for_prompt_ready_before_pasting(monkeypatch: object) -> None:
    backend = CodexBackend()
    calls: list[list[str]] = []
    waited: list[str] = []
    cleared: list[str] = []
    settled: list[tuple[str, str]] = []
    turn_waits: list[str] = []

    monkeypatch.setattr(backend, "_wait_for_prompt_ready", lambda session: waited.append(session))
    monkeypatch.setattr(backend, "_clear_prompt_input", lambda session: cleared.append(session))
    monkeypatch.setattr(backend, "_capture_pane_text", lambda target: "baseline")
    monkeypatch.setattr(backend, "_wait_for_paste_settle", lambda target, baseline: settled.append((target, baseline)))
    monkeypatch.setattr(
        backend,
        "_wait_for_turn_start",
        lambda agent, *, started_after, timeout_seconds=10.0, request_id="": (
            turn_waits.append(f"{agent['tmux_session']}:{timeout_seconds}"),
            TurnObservation(status="running", started_at="2026-04-16T11:43:41.123Z"),
        )[1],
    )

    def fake_run_tmux(args: list[str], check: bool = True) -> SimpleNamespace:
        del check
        calls.append(list(args))
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(backend, "_run_tmux", fake_run_tmux)

    observation = backend.send_prompt({"tmux_session": "flow-agent-9"}, "hello")

    assert waited == ["flow-agent-9"]
    assert cleared == ["flow-agent-9"]
    assert settled == [("flow-agent-9:0.0", "baseline")]
    assert turn_waits == ["flow-agent-9:30.0"]
    assert calls[0][0] == "load-buffer"
    assert calls[1][:4] == ["paste-buffer", "-d", "-t", "flow-agent-9:0.0"]
    assert calls[2] == ["send-keys", "-t", "flow-agent-9:0.0", "Enter"]
    assert observation.started_at == "2026-04-16T11:43:41.123Z"


def test_submit_prompt_retries_enter_until_prompt_disappears(monkeypatch: object) -> None:
    backend = CodexBackend()
    calls: list[list[str]] = []
    pane_texts = iter(
        [
            """
› pasted prompt

  gpt-5.4 xhigh · /localdev/moconnor/mport
""".strip(),
            """
› pasted prompt

  gpt-5.4 xhigh · /localdev/moconnor/mport
""".strip(),
            "• Working (2s • esc to interrupt)",
        ]
    )

    monkeypatch.setattr(backend, "_capture_pane_text", lambda target: next(pane_texts))
    monkeypatch.setattr(backend, "_pane_current_command", lambda target: "node")

    def fake_run_tmux(args: list[str], check: bool = True) -> SimpleNamespace:
        del check
        calls.append(list(args))
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(backend, "_run_tmux", fake_run_tmux)

    backend._submit_prompt("flow-agent-9:0.0")

    assert calls == [
        ["send-keys", "-t", "flow-agent-9:0.0", "Enter"],
        ["send-keys", "-t", "flow-agent-9:0.0", "Enter"],
        ["send-keys", "-t", "flow-agent-9:0.0", "Enter"],
    ]


def test_clear_prompt_input_sends_ctrl_u_and_waits_for_ready(monkeypatch: object) -> None:
    backend = CodexBackend()
    calls: list[list[str]] = []
    waited: list[tuple[str, float]] = []

    def fake_run_tmux(args: list[str], check: bool = True) -> SimpleNamespace:
        del check
        calls.append(list(args))
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(backend, "_run_tmux", fake_run_tmux)
    monkeypatch.setattr(
        backend,
        "_wait_for_prompt_ready",
        lambda session, timeout_seconds=15.0: waited.append((session, timeout_seconds)),
    )

    backend._clear_prompt_input("flow-agent-9", timeout_seconds=3.0)

    assert calls == [["send-keys", "-t", "flow-agent-9:0.0", "C-u"]]
    assert waited == [("flow-agent-9", 3.0)]


def test_visible_prompt_content_returns_empty_for_blank_prompt() -> None:
    text = """
›

  gpt-5.4 xhigh · /localdev/moconnor/mport
""".strip()

    assert _visible_prompt_content(text) == ""


def test_visible_prompt_content_returns_nonempty_prompt_text() -> None:
    text = """
› Work on the following state instructions:
  Prepare the local workspace.

  gpt-5.4 xhigh · /localdev/moconnor/mport
""".strip()

    assert _visible_prompt_content(text) == "Work on the following state instructions:"


def test_set_thread_name_submits_inline_rename_and_waits_for_rollout_event(monkeypatch: object) -> None:
    backend = CodexBackend()
    calls: list[list[str]] = []
    waited: list[str] = []
    cleared: list[str] = []
    settled: list[tuple[str, str]] = []
    captured_texts = iter(["›\n\n  gpt-5.4 xhigh · /localdev/moconnor/mport", "baseline"])

    monkeypatch.setattr(backend, "_wait_for_prompt_ready", lambda session: waited.append(session))
    monkeypatch.setattr(backend, "_clear_prompt_input", lambda session: cleared.append(session))
    monkeypatch.setattr(backend, "_capture_pane_text", lambda target: next(captured_texts))
    monkeypatch.setattr(backend, "_wait_for_paste_settle", lambda target, baseline: settled.append((target, baseline)))
    monkeypatch.setattr(
        backend,
        "_wait_for_thread_rename_event",
        lambda agent, name, started_after, timeout_seconds=10.0: True,
    )

    def fake_run_tmux(args: list[str], check: bool = True) -> SimpleNamespace:
        del check
        calls.append(list(args))
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(backend, "_run_tmux", fake_run_tmux)

    success = backend.set_thread_name({"tmux_session": "flow-agent-9"}, "[flow 9] demo env=prod")

    assert success is True
    assert waited == ["flow-agent-9", "flow-agent-9"]
    assert cleared == ["flow-agent-9"]
    assert settled == [("flow-agent-9:0.0", "baseline")]
    assert calls[0][0] == "load-buffer"
    assert calls[1][:4] == ["paste-buffer", "-d", "-t", "flow-agent-9:0.0"]
    assert calls[2] == ["send-keys", "-t", "flow-agent-9:0.0", "Enter"]


def test_set_thread_name_does_not_skip_ready_placeholder_prompt(monkeypatch: object) -> None:
    backend = CodexBackend()
    calls: list[list[str]] = []
    waited: list[str] = []
    cleared: list[str] = []
    prompt_text = """
› Explain this codebase

  gpt-5.4 xhigh · /localdev/moconnor/mport
""".strip()

    monkeypatch.setattr(backend, "_wait_for_prompt_ready", lambda session: waited.append(session))
    monkeypatch.setattr(backend, "_capture_pane_text", lambda target: "baseline" if cleared else prompt_text)
    monkeypatch.setattr(backend, "_clear_prompt_input", lambda session: cleared.append(session))
    monkeypatch.setattr(backend, "_wait_for_paste_settle", lambda target, baseline: None)
    monkeypatch.setattr(
        backend,
        "_wait_for_thread_rename_event",
        lambda agent, name, started_after, timeout_seconds=10.0: True,
    )

    def fake_run_tmux(args: list[str], check: bool = True) -> SimpleNamespace:
        del check
        calls.append(list(args))
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(backend, "_run_tmux", fake_run_tmux)

    success = backend.set_thread_name({"tmux_session": "flow-agent-9"}, "[flow 9] demo env=prod")

    assert success is True
    assert waited == ["flow-agent-9", "flow-agent-9"]
    assert cleared == ["flow-agent-9"]
    assert calls[0][0] == "load-buffer"
    assert calls[1][:4] == ["paste-buffer", "-d", "-t", "flow-agent-9:0.0"]
    assert calls[2] == ["send-keys", "-t", "flow-agent-9:0.0", "Enter"]


def test_thread_name_updated_seen_matches_rollout_event() -> None:
    events = [
        {
            "timestamp": "2026-04-18T13:11:43.108Z",
            "type": "event_msg",
            "payload": {
                "type": "thread_name_updated",
                "thread_id": "thread-7",
                "thread_name": "[flow 7] block block_family=attention hf_model=TinyLlama/TinyLlama-1.1B-Chat-v1.0",
            },
        }
    ]

    assert _thread_name_updated_seen(
        events,
        expected_name="[flow 7] block block_family=attention hf_model=TinyLlama/TinyLlama-1.1B-Chat-v1.0",
        thread_id="thread-7",
        started_after=datetime(2026, 4, 18, 13, 11, 40, tzinfo=timezone.utc),
    )


def test_poll_turn_keeps_turn_running_until_rollout_explicitly_ends_it(monkeypatch: object) -> None:
    backend = CodexBackend()
    events = [
        {
            "timestamp": "2026-04-18T13:35:51.737Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-1"},
        },
        {
            "timestamp": "2026-04-18T13:35:52.031Z",
            "type": "event_msg",
            "payload": {"type": "token_count"},
        },
    ]
    monkeypatch.setattr(backend, "_resolve_rollout", lambda *args, **kwargs: ("/tmp/fake.jsonl", "thread-1"))
    monkeypatch.setattr("flow.backend._read_rollout_events", lambda path: events)

    observation = backend.poll_turn(
        {
            "tmux_session": "flow-agent-1",
            "thread_id": "thread-1",
            "rollout_path": "/tmp/fake.jsonl",
            "launch_marker": "marker",
            "current_turn_started_at": "2026-04-18T13:35:49.642966Z",
            "current_turn_id": "turn-1",
            "current_request_id": "req-1",
        }
    )

    assert observation.status == "running"
    assert observation.turn_id == "turn-1"


def test_poll_turn_treats_empty_completed_turn_as_completed(monkeypatch: object) -> None:
    backend = CodexBackend()
    events = [
        {
            "timestamp": "2026-04-18T13:35:51.737Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-1"},
        },
        {
            "timestamp": "2026-04-18T13:35:52.031Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "turn-1"},
        },
    ]
    monkeypatch.setattr(backend, "_resolve_rollout", lambda *args, **kwargs: ("/tmp/fake.jsonl", "thread-1"))
    monkeypatch.setattr("flow.backend._read_rollout_events", lambda path: events)

    observation = backend.poll_turn(
        {
            "tmux_session": "flow-agent-1",
            "thread_id": "thread-1",
            "rollout_path": "/tmp/fake.jsonl",
            "launch_marker": "marker",
            "current_turn_started_at": "2026-04-18T13:35:49.642966Z",
            "current_turn_id": "turn-1",
            "current_request_id": "req-1",
        }
    )

    assert observation.status == "completed"
    assert observation.turn_id == "turn-1"


def test_poll_turn_reports_explicit_turn_aborted(monkeypatch: object) -> None:
    backend = CodexBackend()
    events = [
        {
            "timestamp": "2026-04-18T13:35:51.737Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-1"},
        },
        {
            "timestamp": "2026-04-18T13:35:52.031Z",
            "type": "event_msg",
            "payload": {"type": "turn_aborted", "turn_id": "turn-1", "reason": "interrupted"},
        },
    ]
    monkeypatch.setattr(backend, "_resolve_rollout", lambda *args, **kwargs: ("/tmp/fake.jsonl", "thread-1"))
    monkeypatch.setattr("flow.backend._read_rollout_events", lambda path: events)

    observation = backend.poll_turn(
        {
            "tmux_session": "flow-agent-1",
            "thread_id": "thread-1",
            "rollout_path": "/tmp/fake.jsonl",
            "launch_marker": "marker",
            "current_turn_started_at": "2026-04-18T13:35:49.642966Z",
            "current_turn_id": "turn-1",
            "current_request_id": "req-1",
        }
    )

    assert observation.status == "aborted"
    assert observation.turn_id == "turn-1"
    assert observation.abort_reason == "interrupted"


def test_poll_turn_keeps_recent_output_without_task_complete_running(monkeypatch: object) -> None:
    backend = CodexBackend()
    events = [
        {
            "timestamp": "2026-04-18T16:15:00.000Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-2"},
        },
        {
            "timestamp": "2026-04-18T16:15:18.325Z",
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "message": "## Outcome\n- bootstrap-workspace succeeded",
            },
        },
        {
            "timestamp": "2026-04-18T16:15:18.326Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "## Outcome\n- bootstrap-workspace succeeded"}],
            },
        },
    ]
    monkeypatch.setattr(backend, "_resolve_rollout", lambda *args, **kwargs: ("/tmp/fake.jsonl", "thread-2"))
    monkeypatch.setattr("flow.backend._read_rollout_events", lambda path: events)

    observation = backend.poll_turn(
        {
            "tmux_session": "flow-agent-2",
            "thread_id": "thread-2",
            "rollout_path": "/tmp/fake.jsonl",
            "launch_marker": "marker",
            "current_turn_started_at": "2026-04-18T16:15:00.000Z",
            "current_turn_id": "",
            "current_request_id": "",
        }
    )

    assert observation.status == "running"
    assert observation.turn_id == "turn-2"
    assert "bootstrap-workspace succeeded" in observation.output_text


def test_find_turn_matches_overlapping_turn_without_current_turn_id() -> None:
    events = [
        {
            "timestamp": "2026-04-18T10:01:23.738Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-1"},
        },
        {
            "timestamp": "2026-04-18T10:18:12.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Still working."}],
            },
        },
        {
            "timestamp": "2026-04-18T10:25:34.573Z",
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn-1",
                "last_agent_message": "Done.",
            },
        },
    ]

    turn = _find_turn(events, current_turn_id="", current_request_id="", started_after="2026-04-18T10:03:24.000Z")

    assert turn is not None
    assert turn["turn_id"] == "turn-1"
    assert turn["started_at"] == "2026-04-18T10:01:23.738Z"
    assert turn["ended_at"] == "2026-04-18T10:25:34.573Z"
    assert turn["output_text"] == "Done."


def test_find_turn_rejects_completed_turns_wholly_before_started_after() -> None:
    events = [
        {
            "timestamp": "2026-04-18T10:01:23.738Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-1"},
        },
        {
            "timestamp": "2026-04-18T10:02:00.000Z",
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn-1",
                "last_agent_message": "Done.",
            },
        },
    ]

    turn = _find_turn(events, current_turn_id="", current_request_id="", started_after="2026-04-18T10:03:24.000Z")

    assert turn is None


def test_find_turn_prefers_matching_request_id_over_started_after() -> None:
    events = [
        {
            "timestamp": "2026-04-18T12:17:16.344Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-2"},
        },
        {
            "timestamp": "2026-04-18T12:17:16.350Z",
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "[flow-control]\nrequest_id: req-abc123\n[/flow-control]\n\nContinue.",
            },
        },
        {
            "timestamp": "2026-04-18T12:17:45.521Z",
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn-2",
                "last_agent_message": "Prose summary.",
            },
        },
    ]

    turn = _find_turn(
        events,
        current_turn_id="",
        current_request_id="req-abc123",
        started_after="2026-04-18T12:17:45.700Z",
    )

    assert turn is not None
    assert turn["turn_id"] == "turn-2"
    assert turn["request_id"] == "req-abc123"


def test_wait_for_turn_start_accepts_request_ack_without_task_start(monkeypatch: object) -> None:
    backend = CodexBackend()
    events = [
        {
            "timestamp": "2026-04-18T08:16:37.800Z",
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "[flow-control]\nrequest_id: req-123\n[/flow-control]\n\nContinue.",
            },
        }
    ]

    monkeypatch.setattr(backend, "_resolve_rollout", lambda *args, **kwargs: ("/tmp/fake.jsonl", "thread-9"))
    monkeypatch.setattr("flow.backend._read_rollout_events", lambda path: events)

    observation = backend._wait_for_turn_start(  # noqa: SLF001
        {"tmux_session": "flow-agent-9"},
        started_after=parse_utc("2026-04-18T08:16:37Z"),
        timeout_seconds=0.2,
        request_id="req-123",
    )

    assert observation.status == "running"
    assert observation.started_at == "2026-04-18T08:16:37.800Z"


def test_wait_for_turn_start_requires_rollout_acknowledgement(monkeypatch: object) -> None:
    backend = CodexBackend()

    monkeypatch.setattr(backend, "_resolve_rollout", lambda *args, **kwargs: ("", ""))
    monkeypatch.setattr(backend, "_capture_pane_text", lambda target: "")
    monkeypatch.setattr(backend, "_pane_current_command", lambda target: "node")

    with pytest.raises(RuntimeError, match="prompt submission was not acknowledged"):
        backend._wait_for_turn_start(  # noqa: SLF001
            {"tmux_session": "flow-agent-9"},
            started_after=parse_utc("2026-04-18T08:16:37.654321Z"),
            timeout_seconds=0.0,
            request_id="req-123",
        )


def test_wait_for_turn_start_reports_codex_auth_failure_from_pane(monkeypatch: object) -> None:
    backend = CodexBackend()
    pane_text = """
■ Your access token could not be refreshed because your refresh token was
already used. Please log out and sign in again.

› Improve documentation in @filename

  gpt-5.4 xhigh fast
""".strip()

    monkeypatch.setattr(backend, "_resolve_rollout", lambda *args, **kwargs: ("", ""))
    monkeypatch.setattr(backend, "_capture_pane_text", lambda target: pane_text)
    monkeypatch.setattr(backend, "_pane_current_command", lambda target: "codex-aarch64-a")

    with pytest.raises(RuntimeError, match="Codex authentication failed; run `codex logout` and `codex login`"):
        backend._wait_for_turn_start(  # noqa: SLF001
            {"tmux_session": "flow-agent-9"},
            started_after=parse_utc("2026-04-18T08:16:37.654321Z"),
            timeout_seconds=0.0,
            request_id="req-123",
        )


def test_attach_env_unsets_tmux(monkeypatch: object) -> None:
    monkeypatch.setenv("TMUX", "/tmp/tmux-123/default,999,0")
    monkeypatch.setenv("OTHER_ENV", "keep-me")

    env = _attach_env()

    assert "TMUX" not in env
    assert env["OTHER_ENV"] == "keep-me"


def test_viewer_pane_command_is_read_only_nested_attach() -> None:
    backend = CodexBackend()

    command = backend._viewer_pane_command({"tmux_session": "flow-123-agent-7"})

    assert "TMUX=" in command
    assert "attach-session" in command
    assert "-r" in command
    assert "flow-123-agent-7" in command


def test_format_agent_args_formats_sorted_key_values() -> None:
    assert _format_agent_args('{"site":"news.ycombinator.com","mode":"hn"}') == "mode=hn site=news.ycombinator.com"
    assert _format_agent_args("{}") == ""


def test_viewer_pane_title_includes_args_and_path() -> None:
    backend = CodexBackend()

    title = backend._viewer_pane_title(
        {
            "id": 7,
            "flow_name": "agi-watcher",
            "current_state": "check-news",
            "args_json": '{"site":"news.ycombinator.com"}',
            "cwd": "/tmp/agent-flows",
            "substate": "interaction",
        }
    )

    assert title == "#7 agi-watcher:check-news site=news.ycombinator.com /tmp/agent-flows [interaction]"


def test_is_codex_process_name_accepts_codex_variants() -> None:
    assert _is_codex_process_name("codex")
    assert _is_codex_process_name("codex-aarch64-a")
    assert _is_codex_process_name("node")
    assert _is_codex_process_name("nodejs")
    assert not _is_codex_process_name("bash")


def test_ensure_session_relaunches_when_tmux_pane_is_not_running_codex(monkeypatch: object) -> None:
    backend = CodexBackend()
    agent = {
        "tmux_session": "flow-123-agent-7",
        "cwd": "/tmp/work",
        "mode": "yolo",
        "thinking": "low",
        "launch_command": "",
        "thread_id": "",
    }
    calls: list[str] = []

    monkeypatch.setattr(backend, "session_exists", lambda _agent: True)
    monkeypatch.setattr(backend, "_session_has_live_codex", lambda _session: False)
    monkeypatch.setattr(backend, "interrupt", lambda _agent: calls.append("interrupt"))
    monkeypatch.setattr(backend, "_launch_codex", lambda _agent: calls.append("launch"))
    monkeypatch.setattr(backend, "_wait_for_codex_ready", lambda _session: calls.append("wait"))

    result = backend.ensure_session(agent)

    assert calls == ["interrupt", "launch", "wait"]
    assert result["launch_command"] == backend._launch_signature(agent)


def test_session_has_live_codex_rejects_auth_failed_pane(monkeypatch: object) -> None:
    backend = CodexBackend()
    pane_text = """
■ Your access token could not be refreshed because your refresh token was
already used. Please log out and sign in again.
""".strip()

    monkeypatch.setattr(
        "flow.backend.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="codex-aarch64-a\n"),
    )
    monkeypatch.setattr(backend, "_capture_pane_text", lambda target: pane_text)

    assert backend._session_has_live_codex("flow-123-agent-7") is False  # noqa: SLF001


def test_wait_for_codex_ready_checks_ready_state_at_timeout_boundary(monkeypatch: object) -> None:
    backend = CodexBackend()
    ready_text = """
╭────────────────────────────────────────────────────╮
│ >_ OpenAI Codex (v0.121.0)                         │
│                                                    │
│ model:     gpt-5.4 low   /model to change          │
│ directory: /tmp/flow-ready                         │
╰────────────────────────────────────────────────────╯

› Explain this codebase

  gpt-5.4 low · /tmp/flow-ready
""".strip()

    current_time = datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc)
    time_values = iter(
        [
            current_time,
            current_time,
            current_time,
            current_time.replace(microsecond=100_000),
            current_time.replace(microsecond=100_000),
            current_time.replace(microsecond=200_000),
        ]
    )

    def fake_run(args: list[str], capture_output: bool = True, text: bool = True) -> SimpleNamespace:
        del capture_output, text
        if args[:3] == ["tmux", "display-message", "-p"]:
            return SimpleNamespace(returncode=0, stdout="node\n")
        if args[:3] == ["tmux", "capture-pane", "-pt"]:
            return SimpleNamespace(returncode=0, stdout=ready_text)
        raise AssertionError(f"unexpected subprocess args: {args}")

    monkeypatch.setattr("flow.backend.subprocess.run", fake_run)
    monkeypatch.setattr("flow.backend.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("flow.backend.utc_now", lambda: next(time_values))

    backend._wait_for_codex_ready("flow-agent-1", timeout_seconds=0.2)


def test_wait_for_codex_ready_waits_for_visible_prompt_not_just_banner(monkeypatch: object) -> None:
    backend = CodexBackend()
    banner_only = """
╭────────────────────────────────────────────────────╮
│ >_ OpenAI Codex (v0.121.0)                         │
│                                                    │
│ model:     gpt-5.4 low   /model to change          │
│ directory: /tmp/flow-ready                         │
╰────────────────────────────────────────────────────╯
""".strip()
    ready_text = """
╭────────────────────────────────────────────────────╮
│ >_ OpenAI Codex (v0.121.0)                         │
│                                                    │
│ model:     gpt-5.4 low   /model to change          │
│ directory: /tmp/flow-ready                         │
╰────────────────────────────────────────────────────╯

› Explain this codebase

  gpt-5.4 low · /tmp/flow-ready
""".strip()
    capture_values = iter([banner_only, banner_only, ready_text, ready_text])
    current_time = datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc)
    time_values = iter(
        [
            current_time,
            current_time,
            current_time.replace(microsecond=100_000),
            current_time.replace(microsecond=100_000),
            current_time.replace(microsecond=200_000),
            current_time.replace(microsecond=200_000),
            current_time.replace(microsecond=300_000),
            current_time.replace(microsecond=300_000),
            current_time.replace(microsecond=400_000),
        ]
    )

    def fake_run(args: list[str], capture_output: bool = True, text: bool = True) -> SimpleNamespace:
        del capture_output, text
        if args[:3] == ["tmux", "display-message", "-p"]:
            return SimpleNamespace(returncode=0, stdout="node\n")
        if args[:3] == ["tmux", "capture-pane", "-pt"]:
            return SimpleNamespace(returncode=0, stdout=next(capture_values))
        raise AssertionError(f"unexpected subprocess args: {args}")

    monkeypatch.setattr("flow.backend.subprocess.run", fake_run)
    monkeypatch.setattr("flow.backend.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("flow.backend.utc_now", lambda: next(time_values))

    backend._wait_for_codex_ready("flow-agent-1", timeout_seconds=0.4)


def test_wait_for_codex_ready_fails_fast_on_shell_launch_error(monkeypatch: object) -> None:
    backend = CodexBackend()
    error_text = """
(venv) moconnor@host:/localdev/moconnor/mport$ codex --disable tui_app_server --no-alt-screen --cd /localdev/moconnor/mport
bash: codex: command not found
""".strip()

    def fake_run(args: list[str], capture_output: bool = True, text: bool = True) -> SimpleNamespace:
        del capture_output, text
        if args[:3] == ["tmux", "display-message", "-p"]:
            return SimpleNamespace(returncode=0, stdout="bash\n")
        if args[:3] == ["tmux", "capture-pane", "-pt"]:
            return SimpleNamespace(returncode=0, stdout=error_text)
        raise AssertionError(f"unexpected subprocess args: {args}")

    monkeypatch.setattr("flow.backend.subprocess.run", fake_run)
    monkeypatch.setattr("flow.backend.time.sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="Codex failed to launch"):
        backend._wait_for_codex_ready("flow-agent-1", timeout_seconds=30.0)


def test_codex_launch_failure_reason_detects_shell_missing_binary() -> None:
    text = """
(venv) moconnor@host:/localdev/moconnor/mport$ codex --disable tui_app_server --no-alt-screen --cd /localdev/moconnor/mport
bash: codex: command not found
""".strip()

    assert _codex_launch_failure_reason(text, current_command="bash") == "bash: codex: command not found"


def test_attach_many_sizes_viewer_session_and_resizes_nested_sessions(monkeypatch: object) -> None:
    backend = CodexBackend()
    calls: list[list[str]] = []
    split_panes = iter(["%2\n", "%3\n"])

    def fake_run_tmux(args: list[str], check: bool = True) -> SimpleNamespace:
        del check
        calls.append(list(args))
        if args[0] == "new-session":
            return SimpleNamespace(stdout="%1\n")
        if args[0] == "split-window":
            return SimpleNamespace(stdout=next(split_panes))
        if args[0] == "display-message":
            target = args[3]
            sizes = {"%1": "90x24\n", "%2": "89x24\n", "%3": "180x23\n"}
            return SimpleNamespace(stdout=sizes.get(target, "80x24\n"))
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(backend, "session_exists", lambda _agent: True)
    monkeypatch.setattr(backend, "_viewer_session_name", lambda: "flow-view-test")
    monkeypatch.setattr(backend, "_terminal_size", lambda: (180, 50))
    monkeypatch.setattr(backend, "_run_tmux", fake_run_tmux)
    monkeypatch.setattr("flow.backend.subprocess.call", lambda *args, **kwargs: 0)

    agents = [
        {"id": 3, "flow_name": "demo", "current_state": "check", "tmux_session": "flow-agent-3", "cwd": "/tmp/a", "substate": "normal"},
        {"id": 4, "flow_name": "demo", "current_state": "check", "tmux_session": "flow-agent-4", "cwd": "/tmp/b", "substate": "normal"},
        {"id": 5, "flow_name": "demo", "current_state": "check", "tmux_session": "flow-agent-5", "cwd": "/tmp/c", "substate": "normal"},
    ]

    assert backend.attach_many(agents) == 0

    assert ["new-session", "-d", "-P", "-F", "#{pane_id}", "-s", "flow-view-test", "-n", "flow-view", "-x", "180", "-y", "50", "-c", "/tmp/a", backend._viewer_pane_command(agents[0])] in calls
    assert ["resize-window", "-t", "flow-view-test:0", "-x", "180", "-y", "50"] in calls
    assert ["resize-window", "-t", "flow-agent-3:0", "-x", "90", "-y", "24"] in calls
    assert ["resize-window", "-t", "flow-agent-4:0", "-x", "89", "-y", "24"] in calls
    assert ["resize-window", "-t", "flow-agent-5:0", "-x", "180", "-y", "23"] in calls
    assert ["set-window-option", "-t", "flow-agent-3:0", "window-size", "latest"] in calls
    assert ["set-window-option", "-t", "flow-agent-3:0", "aggressive-resize", "on"] in calls
    assert ["set-window-option", "-t", "flow-agent-4:0", "window-size", "latest"] in calls
    assert ["set-window-option", "-t", "flow-agent-4:0", "aggressive-resize", "on"] in calls
    assert ["set-window-option", "-t", "flow-agent-5:0", "window-size", "latest"] in calls
    assert ["set-window-option", "-t", "flow-agent-5:0", "aggressive-resize", "on"] in calls


def test_attach_restores_session_resize_behavior_before_attaching(monkeypatch: object) -> None:
    backend = CodexBackend()
    calls: list[list[str]] = []

    def fake_run_tmux(args: list[str], check: bool = True) -> SimpleNamespace:
        del check
        calls.append(list(args))
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(backend, "_run_tmux", fake_run_tmux)
    monkeypatch.setattr("flow.backend.subprocess.call", lambda *args, **kwargs: 0)

    agent = {
        "tmux_session": "flow-agent-3",
        "flow_name": "agi-watcher",
        "current_state": "check-news",
        "args_json": '{"site":"news.ycombinator.com"}',
        "cwd": "/tmp/agent-flows",
    }

    assert backend.attach(agent) == 0

    assert calls[:5] == [
        ["set-window-option", "-t", "flow-agent-3:0", "automatic-rename", "off"],
        ["rename-window", "-t", "flow-agent-3:0", "agi-watcher:check-news site=news.ycombinator.com /tmp/agent-flows"],
        ["select-pane", "-t", "flow-agent-3:0.0", "-T", "agi-watcher:check-news site=news.ycombinator.com /tmp/agent-flows"],
        ["set-window-option", "-t", "flow-agent-3:0", "window-size", "latest"],
        ["set-window-option", "-t", "flow-agent-3:0", "aggressive-resize", "on"],
    ]
