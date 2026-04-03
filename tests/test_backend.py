from __future__ import annotations

from types import SimpleNamespace

from flow.backend import (
    CodexBackend,
    _attach_env,
    _format_agent_args,
    _is_codex_process_name,
    _looks_like_codex_prompt_ready,
    _looks_like_codex_trust_prompt,
    _looks_like_codex_tui_ready,
    _session_env_unset_names,
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


def test_launch_command_disables_app_server_tui() -> None:
    backend = CodexBackend()
    command = backend._launch_command({"cwd": "/tmp/work", "mode": "yolo", "thinking": "xhigh"})

    assert "--disable tui_app_server" in command
    assert "-c trust_level=trusted" in command


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


def test_launch_signature_ignores_thread_resume_suffix() -> None:
    backend = CodexBackend()
    agent = {"cwd": "/tmp/work", "mode": "yolo", "thinking": "xhigh", "thread_id": "thread-123"}

    assert "resume thread-123" in backend._launch_command(agent)
    assert "resume" not in backend._launch_signature(agent)


def test_send_prompt_waits_for_prompt_ready_before_pasting(monkeypatch: object) -> None:
    backend = CodexBackend()
    calls: list[list[str]] = []
    waited: list[str] = []

    monkeypatch.setattr(backend, "_wait_for_prompt_ready", lambda session: waited.append(session))

    def fake_run_tmux(args: list[str], check: bool = True) -> SimpleNamespace:
        del check
        calls.append(list(args))
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(backend, "_run_tmux", fake_run_tmux)

    backend.send_prompt({"tmux_session": "flow-agent-9"}, "hello")

    assert waited == ["flow-agent-9"]
    assert calls[0][0] == "load-buffer"
    assert calls[1][:4] == ["paste-buffer", "-d", "-t", "flow-agent-9:0.0"]
    assert calls[2] == ["send-keys", "-t", "flow-agent-9:0.0", "Enter"]


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
    assert not _is_codex_process_name("bash")


def test_ensure_session_relaunches_when_tmux_pane_is_not_running_codex(monkeypatch: object) -> None:
    backend = CodexBackend()
    agent = {
        "tmux_session": "flow-123-agent-7",
        "cwd": "/tmp/work",
        "mode": "read-only",
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
