"""Backend abstractions and the Codex tmux backend."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import format_utc, parse_utc, utc_now


@dataclass(frozen=True)
class TurnObservation:
    status: str
    thread_id: str = ""
    rollout_path: str = ""
    turn_id: str = ""
    started_at: str = ""
    ended_at: str = ""
    output_text: str = ""
    raw_output: str = ""
    last_event_at: str = ""


class AgentBackend(ABC):
    @abstractmethod
    def ensure_session(self, agent: dict[str, Any]) -> dict[str, str]:
        raise NotImplementedError

    @abstractmethod
    def send_prompt(self, agent: dict[str, Any], prompt: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def interrupt(self, agent: dict[str, Any]) -> None:
        raise NotImplementedError

    @abstractmethod
    def terminate(self, agent: dict[str, Any], *, immediate: bool) -> None:
        raise NotImplementedError

    @abstractmethod
    def attach(self, agent: dict[str, Any]) -> int:
        raise NotImplementedError

    @abstractmethod
    def attach_many(self, agents: list[dict[str, Any]]) -> int:
        raise NotImplementedError

    @abstractmethod
    def session_exists(self, agent: dict[str, Any]) -> bool:
        raise NotImplementedError

    @abstractmethod
    def poll_turn(self, agent: dict[str, Any]) -> TurnObservation:
        raise NotImplementedError


class CodexBackend(AgentBackend):
    def ensure_session(self, agent: dict[str, Any]) -> dict[str, str]:
        session = agent["tmux_session"]
        cwd = agent["cwd"]
        if not self.session_exists(agent):
            shell = os.environ.get("SHELL", "/bin/bash")
            self._run_tmux(self._new_session_command(session, cwd, shell))
            self._wait_for_session(session)
            self._sanitize_tmux_session_environment(session)
            self._launch_codex(agent)
            self._wait_for_codex_ready(session)
            return {"launch_command": self._launch_signature(agent), "thread_id": agent.get("thread_id", "")}

        desired = self._launch_signature(agent)
        if not self._session_has_live_codex(session) or agent.get("launch_command") != desired:
            self.interrupt(agent)
            self._launch_codex(agent)
            self._wait_for_codex_ready(session)
        return {"launch_command": desired, "thread_id": agent.get("thread_id", "")}

    def send_prompt(self, agent: dict[str, Any], prompt: str) -> None:
        target = f"{agent['tmux_session']}:0.0"
        self._wait_for_prompt_ready(agent["tmux_session"])
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            handle.write(prompt)
            buffer_path = handle.name
        try:
            self._run_tmux(["load-buffer", buffer_path])
            self._run_tmux(["paste-buffer", "-d", "-t", target])
            # The standalone Codex TUI debounces paste bursts; an immediate Enter
            # can be consumed by the composer before the pasted prompt settles.
            time.sleep(1.0)
            self._run_tmux(["send-keys", "-t", target, "Enter"])
        finally:
            try:
                os.unlink(buffer_path)
            except FileNotFoundError:
                pass

    def interrupt(self, agent: dict[str, Any]) -> None:
        if self.session_exists(agent):
            self._run_tmux(["send-keys", "-t", f"{agent['tmux_session']}:0.0", "C-c"], check=False)

    def terminate(self, agent: dict[str, Any], *, immediate: bool) -> None:
        session = agent["tmux_session"]
        if not self.session_exists(agent):
            return
        if immediate:
            self.interrupt(agent)
        self._run_tmux(["kill-session", "-t", session], check=False)

    def attach(self, agent: dict[str, Any]) -> int:
        self._apply_view_metadata(agent)
        self._restore_session_resize_behavior(agent["tmux_session"])
        return subprocess.call(["tmux", "attach-session", "-t", agent["tmux_session"]], env=_attach_env())

    def attach_many(self, agents: list[dict[str, Any]]) -> int:
        if not agents:
            raise ValueError("attach_many requires at least one agent")
        if len(agents) == 1:
            return self.attach(agents[0])

        missing = [str(agent["id"]) for agent in agents if not self.session_exists(agent)]
        if missing:
            joined = ", ".join(missing)
            raise RuntimeError(f"missing tmux session for agent(s): {joined}")

        session_name = self._viewer_session_name()
        first = agents[0]
        viewer_cols, viewer_rows = self._terminal_size()
        pane_map: list[tuple[dict[str, Any], str]] = []
        try:
            for agent in agents:
                self._apply_view_metadata(agent)
            first_pane = (
                self._run_tmux(
                    [
                        "new-session",
                        "-d",
                        "-P",
                        "-F",
                        "#{pane_id}",
                        "-s",
                        session_name,
                        "-n",
                        "flow-view",
                        "-x",
                        str(viewer_cols),
                        "-y",
                        str(viewer_rows),
                        "-c",
                        first["cwd"],
                        self._viewer_pane_command(first),
                    ]
                )
                .stdout.strip()
            )
            self._run_tmux(
                ["resize-window", "-t", f"{session_name}:0", "-x", str(viewer_cols), "-y", str(viewer_rows)],
                check=False,
            )
            self._configure_viewer_session(session_name)
            if first_pane:
                pane_map.append((first, first_pane))
                self._run_tmux(["select-pane", "-t", first_pane, "-T", self._viewer_pane_title(first)], check=False)

            for agent in agents[1:]:
                pane_id = (
                    self._run_tmux(
                        [
                            "split-window",
                            "-P",
                            "-F",
                            "#{pane_id}",
                            "-t",
                            f"{session_name}:0",
                            "-c",
                            agent["cwd"],
                            self._viewer_pane_command(agent),
                        ]
                    )
                    .stdout.strip()
                )
                if pane_id:
                    pane_map.append((agent, pane_id))
                    self._run_tmux(["select-pane", "-t", pane_id, "-T", self._viewer_pane_title(agent)], check=False)

            self._run_tmux(["select-layout", "-t", f"{session_name}:0", "tiled"], check=False)
            self._resize_viewed_sessions(pane_map)
            self._run_tmux(["select-pane", "-t", f"{session_name}:0.0"], check=False)
            return subprocess.call(["tmux", "attach-session", "-t", session_name], env=_attach_env())
        finally:
            for agent in agents:
                self._restore_session_resize_behavior(str(agent["tmux_session"]))
            self._run_tmux(["kill-session", "-t", session_name], check=False)

    def session_exists(self, agent: dict[str, Any]) -> bool:
        result = subprocess.run(
            ["tmux", "has-session", "-t", agent["tmux_session"]],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def poll_turn(self, agent: dict[str, Any]) -> TurnObservation:
        thread_id = agent.get("thread_id", "") or ""
        rollout_path = agent.get("rollout_path", "") or ""
        launch_marker = agent.get("launch_marker", "") or ""
        turn_started_at = agent.get("current_turn_started_at", "") or ""
        current_turn_id = agent.get("current_turn_id", "") or ""

        resolved_path, resolved_thread_id = self._resolve_rollout(thread_id, rollout_path, launch_marker, turn_started_at)
        if not resolved_path:
            return TurnObservation(status="pending")

        events = _read_rollout_events(Path(resolved_path))
        if not events:
            return TurnObservation(status="pending", thread_id=resolved_thread_id, rollout_path=resolved_path)

        turn = _find_turn(events, current_turn_id=current_turn_id, started_after=turn_started_at)
        if turn is None:
            return TurnObservation(status="pending", thread_id=resolved_thread_id, rollout_path=resolved_path)
        if not turn["ended_at"]:
            return TurnObservation(
                status="running",
                thread_id=resolved_thread_id,
                rollout_path=resolved_path,
                turn_id=turn["turn_id"],
                started_at=turn["started_at"],
                last_event_at=turn["last_event_at"],
            )
        return TurnObservation(
            status="completed",
            thread_id=resolved_thread_id,
            rollout_path=resolved_path,
            turn_id=turn["turn_id"],
            started_at=turn["started_at"],
            ended_at=turn["ended_at"],
            output_text=turn["output_text"],
            raw_output=turn["raw_output"],
            last_event_at=turn["last_event_at"],
        )

    def _launch_codex(self, agent: dict[str, Any]) -> None:
        target = f"{agent['tmux_session']}:0.0"
        self._run_tmux(["send-keys", "-t", target, "C-c"], check=False)
        self._run_tmux(["send-keys", "-t", target, "Enter"], check=False)
        self._run_tmux(["send-keys", "-t", target, "C-l"], check=False)
        self._run_tmux(["send-keys", "-t", target, self._launch_command(agent), "Enter"])

    def _launch_command(self, agent: dict[str, Any]) -> str:
        parts = ["codex", "--disable", "tui_app_server", "--no-alt-screen", "--cd", shlex.quote(agent["cwd"])]
        mode = agent.get("desired_mode") or agent.get("mode") or "yolo"
        thinking = agent.get("desired_thinking") or agent.get("thinking") or "xhigh"
        if mode == "yolo":
            parts.append("--dangerously-bypass-approvals-and-sandbox")
        elif mode == "full-auto":
            parts.append("--full-auto")
        elif mode == "workspace-write":
            parts.extend(["-a", "on-request", "-s", "workspace-write"])
        elif mode == "read-only":
            parts.extend(["-a", "on-request", "-s", "read-only"])
        elif mode == "danger-full-access":
            parts.extend(["-a", "never", "-s", "danger-full-access"])
        parts.extend(["-c", shlex.quote("trust_level=trusted")])
        parts.extend(["-c", shlex.quote(f"model_reasoning_effort={thinking}")])
        parts.extend(["-c", shlex.quote("check_for_update_on_startup=false")])
        thread_id = agent.get("thread_id") or ""
        if thread_id:
            parts.extend(["resume", shlex.quote(thread_id)])
        return " ".join(parts)

    def _launch_signature(self, agent: dict[str, Any]) -> str:
        parts = ["codex", "--disable", "tui_app_server", "--no-alt-screen", "--cd", shlex.quote(agent["cwd"])]
        mode = agent.get("desired_mode") or agent.get("mode") or "yolo"
        thinking = agent.get("desired_thinking") or agent.get("thinking") or "xhigh"
        if mode == "yolo":
            parts.append("--dangerously-bypass-approvals-and-sandbox")
        elif mode == "full-auto":
            parts.append("--full-auto")
        elif mode == "workspace-write":
            parts.extend(["-a", "on-request", "-s", "workspace-write"])
        elif mode == "read-only":
            parts.extend(["-a", "on-request", "-s", "read-only"])
        elif mode == "danger-full-access":
            parts.extend(["-a", "never", "-s", "danger-full-access"])
        parts.extend(["-c", shlex.quote("trust_level=trusted")])
        parts.extend(["-c", shlex.quote(f"model_reasoning_effort={thinking}")])
        parts.extend(["-c", shlex.quote("check_for_update_on_startup=false")])
        return " ".join(parts)

    def _run_tmux(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(["tmux", *args], capture_output=True, text=True)
        if check and result.returncode != 0:
            stderr = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"tmux {' '.join(args)} failed: {stderr}")
        return result

    def _new_session_command(self, session: str, cwd: str, shell: str) -> list[str]:
        command = ["new-session", "-d", "-s", session, "-c", cwd, "env"]
        for name in _session_env_unset_names():
            command.extend(["-u", name])
        command.extend([shell, "-l"])
        return command

    def _sanitize_tmux_session_environment(self, session: str) -> None:
        for name in _session_env_unset_names():
            self._run_tmux(["set-environment", "-t", session, "-u", name], check=False)

    def _wait_for_session(self, session: str, timeout_seconds: float = 2.0) -> None:
        deadline = utc_now().timestamp() + timeout_seconds
        while utc_now().timestamp() < deadline:
            if subprocess.run(["tmux", "has-session", "-t", session], capture_output=True, text=True).returncode == 0:
                return
            import time

            time.sleep(0.05)
        raise RuntimeError(f"tmux session '{session}' did not stay alive after creation")

    def _wait_for_codex_ready(self, session: str, timeout_seconds: float = 30.0) -> None:
        import time

        deadline = utc_now().timestamp() + timeout_seconds
        target = f"{session}:0.0"
        last_trust_confirm_at = 0.0
        while utc_now().timestamp() < deadline:
            current = subprocess.run(
                ["tmux", "display-message", "-p", "-t", target, "#{pane_current_command}"],
                capture_output=True,
                text=True,
            )
            capture = subprocess.run(
                ["tmux", "capture-pane", "-pt", target, "-S", "-80"],
                capture_output=True,
                text=True,
            )
            if capture.returncode == 0:
                text = capture.stdout or ""
                current_command = (current.stdout or "").strip()
                if _looks_like_codex_trust_prompt(text, current_command=current_command):
                    now_ts = utc_now().timestamp()
                    if now_ts - last_trust_confirm_at >= 1.0:
                        self._run_tmux(["send-keys", "-t", target, "Enter"], check=False)
                        last_trust_confirm_at = now_ts
                    time.sleep(0.2)
                    continue
                if _looks_like_codex_tui_ready(text, current_command=current_command):
                    return
            time.sleep(0.1)
        raise RuntimeError(f"Codex did not become ready in tmux session '{session}'")

    def _wait_for_prompt_ready(self, session: str, timeout_seconds: float = 15.0) -> None:
        import time

        deadline = utc_now().timestamp() + timeout_seconds
        target = f"{session}:0.0"
        while utc_now().timestamp() < deadline:
            current = subprocess.run(
                ["tmux", "display-message", "-p", "-t", target, "#{pane_current_command}"],
                capture_output=True,
                text=True,
            )
            capture = subprocess.run(
                ["tmux", "capture-pane", "-pt", target, "-S", "-80"],
                capture_output=True,
                text=True,
            )
            if capture.returncode == 0:
                text = capture.stdout or ""
                current_command = (current.stdout or "").strip()
                if _looks_like_codex_prompt_ready(text, current_command=current_command):
                    return
            time.sleep(0.1)
        raise RuntimeError(f"Codex prompt input did not become ready in tmux session '{session}'")

    def _session_has_live_codex(self, session: str) -> bool:
        target = f"{session}:0.0"
        current = subprocess.run(
            ["tmux", "display-message", "-p", "-t", target, "#{pane_current_command}"],
            capture_output=True,
            text=True,
        )
        if current.returncode != 0:
            return False
        return _is_codex_process_name((current.stdout or "").strip())

    def _resolve_rollout(
        self,
        thread_id: str,
        rollout_path: str,
        launch_marker: str,
        turn_started_at: str,
    ) -> tuple[str, str]:
        if rollout_path and Path(rollout_path).exists():
            path = Path(rollout_path)
            return str(path), thread_id or _thread_id_from_rollout(path)

        root = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "sessions"
        if not root.exists():
            return "", thread_id

        if thread_id:
            candidates = sorted(root.rglob(f"*{thread_id}*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True)
            for path in candidates:
                if path.name.startswith("rollout-"):
                    return str(path), thread_id

        marker_time = parse_utc(turn_started_at) or utc_now()
        best: list[tuple[float, Path]] = []
        for path in root.rglob("rollout-*.jsonl"):
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime < marker_time.timestamp() - 300:
                continue
            best.append((stat.st_mtime, path))
        for _mtime, path in sorted(best, reverse=True)[:200]:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if launch_marker and launch_marker in text:
                return str(path), _thread_id_from_rollout(path)
        return "", thread_id

    def _viewer_session_name(self) -> str:
        return f"flow-view-{os.getpid()}-{uuid.uuid4().hex[:6]}"

    def _viewer_pane_command(self, agent: dict[str, Any]) -> str:
        return shlex.join(["env", "TMUX=", "tmux", "attach-session", "-r", "-t", str(agent["tmux_session"])])

    def _viewer_pane_title(self, agent: dict[str, Any]) -> str:
        substate = str(agent.get("substate") or "").strip()
        suffix = "" if not substate or substate == "normal" else f" [{substate}]"
        return f"#{agent['id']} {self._view_label(agent)}{suffix}"

    def _configure_viewer_session(self, session_name: str) -> None:
        for args in (
            ["set-option", "-t", session_name, "status", "off"],
            ["set-option", "-t", session_name, "mouse", "on"],
            ["set-window-option", "-t", f"{session_name}:0", "pane-border-status", "top"],
            ["set-window-option", "-t", f"{session_name}:0", "pane-border-format", "#{pane_title}"],
            ["set-window-option", "-t", f"{session_name}:0", "window-size", "latest"],
            ["set-window-option", "-t", f"{session_name}:0", "aggressive-resize", "on"],
        ):
            self._run_tmux(args, check=False)

    def _terminal_size(self) -> tuple[int, int]:
        size = shutil.get_terminal_size(fallback=(120, 40))
        return max(40, int(size.columns)), max(10, int(size.lines))

    def _resize_viewed_sessions(self, pane_map: list[tuple[dict[str, Any], str]]) -> None:
        for agent, pane_id in pane_map:
            size = self._run_tmux(
                ["display-message", "-p", "-t", pane_id, "#{pane_width}x#{pane_height}"],
                check=False,
            ).stdout.strip()
            if "x" not in size:
                continue
            width_text, height_text = size.split("x", 1)
            try:
                width = max(20, int(width_text))
                height = max(5, int(height_text))
            except ValueError:
                continue
            self._run_tmux(
                ["resize-window", "-t", f"{agent['tmux_session']}:0", "-x", str(width), "-y", str(height)],
                check=False,
            )

    def _restore_session_resize_behavior(self, session_name: str) -> None:
        for args in (
            ["set-window-option", "-t", f"{session_name}:0", "window-size", "latest"],
            ["set-window-option", "-t", f"{session_name}:0", "aggressive-resize", "on"],
        ):
            self._run_tmux(args, check=False)

    def _apply_view_metadata(self, agent: dict[str, Any]) -> None:
        label = self._view_label(agent)
        target = f"{agent['tmux_session']}:0"
        self._run_tmux(["set-window-option", "-t", target, "automatic-rename", "off"], check=False)
        self._run_tmux(["rename-window", "-t", target, label], check=False)
        self._run_tmux(["select-pane", "-t", f"{target}.0", "-T", label], check=False)

    def _view_label(self, agent: dict[str, Any]) -> str:
        parts = [f"{agent['flow_name']}:{agent['current_state']}"]
        args_text = _format_agent_args(agent.get("args_json", ""))
        if args_text:
            parts.append(args_text)
        cwd = str(agent.get("cwd") or "").strip()
        if cwd:
            parts.append(cwd)
        return " ".join(parts)


def _thread_id_from_rollout(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                payload = event.get("payload") or {}
                if event.get("type") == "session_meta":
                    session_id = payload.get("id")
                    if isinstance(session_id, str):
                        return session_id
    except (OSError, json.JSONDecodeError):
        return ""
    return ""


def _attach_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("TMUX", None)
    return env


def _is_codex_process_name(value: str) -> bool:
    text = value.strip().lower()
    return text.startswith("codex")


def _format_agent_args(text: str) -> str:
    try:
        payload = json.loads(text or "{}")
    except Exception:
        return ""
    if not isinstance(payload, dict) or not payload:
        return ""
    return " ".join(f"{key}={value}" for key, value in sorted(payload.items()))


def _read_rollout_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
    except OSError:
        return []
    return events


def _looks_like_codex_trust_prompt(text: str, *, current_command: str = "") -> bool:
    lower = text.lower()
    return _is_codex_process_name(current_command) and "do you trust the contents of this directory?" in lower


def _find_turn(
    events: list[dict[str, Any]],
    *,
    current_turn_id: str,
    started_after: str,
) -> dict[str, str] | None:
    started_after_dt = parse_utc(started_after)
    candidate: dict[str, str] | None = None
    bucket: list[dict[str, Any]] = []

    for event in events:
        payload = event.get("payload") or {}
        if event.get("type") == "event_msg" and payload.get("type") == "task_started":
            turn_id = str(payload.get("turn_id") or "")
            if current_turn_id and turn_id != current_turn_id:
                bucket = []
                continue
            timestamp = parse_utc(event.get("timestamp"))
            if not current_turn_id and started_after_dt is not None and timestamp is not None:
                if timestamp < started_after_dt:
                    bucket = []
                    continue
            bucket = [event]
            candidate = {
                "turn_id": turn_id,
                "started_at": event.get("timestamp") or "",
                "ended_at": "",
                "output_text": "",
                "raw_output": "",
                "last_event_at": event.get("timestamp") or "",
            }
            continue
        if bucket:
            bucket.append(event)
            if candidate is not None:
                candidate["last_event_at"] = event.get("timestamp") or candidate["last_event_at"]

    if not bucket or candidate is None:
        return None

    assistant_messages: list[str] = []
    for event in bucket:
        payload = event.get("payload") or {}
        if event.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "assistant":
            text = _assistant_text(payload)
            if text:
                assistant_messages.append(text)
                candidate["raw_output"] = text
        elif event.get("type") == "event_msg" and payload.get("type") == "task_complete":
            candidate["ended_at"] = event.get("timestamp") or ""
            last_message = payload.get("last_agent_message")
            if isinstance(last_message, str) and last_message.strip():
                candidate["output_text"] = last_message.strip()
    if not candidate["output_text"] and assistant_messages:
        candidate["output_text"] = assistant_messages[-1]
    return candidate


def _assistant_text(payload: dict[str, Any]) -> str:
    content = payload.get("content") or []
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "output_text" and item.get("text"):
            parts.append(str(item["text"]))
    return "\n".join(part.strip() for part in parts if str(part).strip()).strip()


def _session_env_unset_names() -> list[str]:
    names: set[str] = set()
    for key in os.environ:
        if key.startswith("CHATGPT_"):
            names.add(key)
        elif key.startswith("CODEX_") and key != "CODEX_HOME":
            names.add(key)
    names.add("__CFBundleIdentifier")
    return sorted(names)


def _looks_like_codex_tui_ready(text: str, *, current_command: str = "") -> bool:
    command = current_command.strip().lower()
    if "OpenAI Codex" in text and any(marker in text for marker in ("model:", "directory:", "gpt-5.4", "gpt-5")):
        return True

    # After startup or restore, Codex may already be in an active conversation
    # view where the initial banner has scrolled away. In that case the tmux
    # pane still belongs to the codex process and contains our conversation UI.
    if command.startswith("codex"):
        if "[flow-control]" in text:
            return True
        if any(marker in text for marker in ("› ", "• ", "Run /", "gpt-5.4", "gpt-5", "model:", "directory:")):
            return True
    return False


def _looks_like_codex_prompt_ready(text: str, *, current_command: str = "") -> bool:
    if not _is_codex_process_name(current_command):
        return False
    if _looks_like_codex_trust_prompt(text, current_command=current_command):
        return False

    if "OpenAI Codex" in text and any(marker in text for marker in ("model:", "directory:", "gpt-5.4", "gpt-5")):
        return True

    lines = [line.rstrip() for line in text.splitlines()]
    tail = [line.strip() for line in lines[-14:] if line.strip()]
    if not tail:
        return False
    if not any("gpt-" in line.lower() or "model:" in line.lower() for line in tail):
        return False

    for line in reversed(tail):
        if not line.startswith("› "):
            continue
        content = line[2:].strip()
        if not content:
            return True
        if content.startswith("[flow-control]"):
            continue
        return True
    return False
