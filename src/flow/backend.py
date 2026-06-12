"""Backend abstractions and the Codex tmux backend."""

from __future__ import annotations

import json
import os
import pwd
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .common import format_utc, parse_utc, pending_state_payload, utc_now
from .scratchpad import ensure_scratchpad_dir

_SUBMIT_RETRY_INTERVAL_SECONDS = 1.5


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
    abort_reason: str = ""
class AgentBackend(ABC):
    @abstractmethod
    def ensure_session(self, agent: dict[str, Any]) -> dict[str, str]:
        raise NotImplementedError

    @abstractmethod
    def send_prompt(self, agent: dict[str, Any], prompt: str, *, request_id: str = "") -> TurnObservation:
        raise NotImplementedError

    def set_thread_name(self, agent: dict[str, Any], name: str) -> bool | None:
        del agent, name
        return None

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

    def send_prompt(self, agent: dict[str, Any], prompt: str, *, request_id: str = "") -> TurnObservation:
        target = f"{agent['tmux_session']}:0.0"
        self._ensure_codex_prompt_ready(agent)
        self._clear_prompt_input(agent["tmux_session"])
        baseline = self._capture_pane_text(target)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            handle.write(prompt)
            buffer_path = handle.name
        try:
            self._paste_prompt(target, buffer_path, baseline)
            submitted_at = utc_now()
            self._submit_prompt(target)
            return self._wait_for_turn_start(
                agent,
                started_after=submitted_at,
                timeout_seconds=30.0,
                request_id=request_id,
                prompt_text=prompt,
            )
        finally:
            try:
                os.unlink(buffer_path)
            except FileNotFoundError:
                pass

    def set_thread_name(self, agent: dict[str, Any], name: str) -> bool | None:
        session = agent["tmux_session"]
        target = f"{session}:0.0"
        sanitized = _sanitize_thread_name(name)
        if not sanitized:
            return False

        self._ensure_codex_prompt_ready(agent)
        baseline = self._capture_pane_text(target)
        self._clear_prompt_input(session)
        baseline = self._capture_pane_text(target)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            handle.write(f"/rename {sanitized}")
            buffer_path = handle.name
        try:
            self._run_tmux(["load-buffer", buffer_path])
            self._paste_loaded_buffer(target)
            self._wait_for_paste_settle(target, baseline)
            submitted_at = utc_now()
            self._run_tmux(["send-keys", "-t", target, "Enter"])
            if not self._wait_for_thread_rename_event(agent, sanitized, started_after=submitted_at):
                return False
            self._wait_for_prompt_ready(session)
            return True
        except RuntimeError:
            return False
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
        current_request_id = agent.get("current_request_id", "") or ""

        resolved_path, resolved_thread_id = self._resolve_rollout(thread_id, rollout_path, launch_marker, turn_started_at)
        if not resolved_path:
            permission_prompt = self._permission_prompt_reason(agent)
            if permission_prompt:
                raise RuntimeError(permission_prompt)
            return TurnObservation(status="pending")

        events = _read_rollout_events(Path(resolved_path))
        if not events:
            permission_prompt = self._permission_prompt_reason(agent)
            if permission_prompt:
                raise RuntimeError(permission_prompt)
            return TurnObservation(status="pending", thread_id=resolved_thread_id, rollout_path=resolved_path)
        request_acknowledged_at = _request_acknowledged_at(
            events,
            request_id=current_request_id,
            started_after=turn_started_at,
        )

        turn = _find_turn(
            events,
            current_turn_id=current_turn_id,
            current_request_id=current_request_id,
            started_after=turn_started_at,
        )
        if turn is None:
            if request_acknowledged_at:
                permission_prompt = self._permission_prompt_reason(agent)
                if permission_prompt:
                    raise RuntimeError(permission_prompt)
                return TurnObservation(
                    status="running",
                    thread_id=resolved_thread_id,
                    rollout_path=resolved_path,
                    started_at=request_acknowledged_at,
                    last_event_at=request_acknowledged_at,
                )
            return TurnObservation(status="pending", thread_id=resolved_thread_id, rollout_path=resolved_path)
        if turn["status"] == "aborted":
            return TurnObservation(
                status="aborted",
                thread_id=resolved_thread_id,
                rollout_path=resolved_path,
                turn_id=turn["turn_id"],
                started_at=turn["started_at"],
                ended_at=turn["ended_at"],
                output_text=turn["output_text"],
                raw_output=turn["raw_output"],
                last_event_at=turn["last_event_at"],
                abort_reason=turn["abort_reason"],
            )
        if turn["status"] != "completed":
            permission_prompt = self._permission_prompt_reason(agent)
            if permission_prompt:
                raise RuntimeError(permission_prompt)
            return TurnObservation(
                status="running",
                thread_id=resolved_thread_id,
                rollout_path=resolved_path,
                turn_id=turn["turn_id"],
                started_at=turn["started_at"] or request_acknowledged_at,
                output_text=turn["output_text"],
                raw_output=turn["raw_output"],
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
        baseline = self._capture_pane_text(target)
        self._paste_text(target, self._launch_command(agent), baseline=baseline, bracketed=False)
        self._run_tmux(["send-keys", "-t", target, "Enter"])

    def _launch_command(self, agent: dict[str, Any]) -> str:
        parts = self._launch_parts(agent)
        thread_id = agent.get("thread_id") or ""
        if thread_id:
            parts.extend(["resume", shlex.quote(thread_id)])
        return " ".join(parts)

    def _launch_signature(self, agent: dict[str, Any]) -> str:
        return " ".join(self._launch_parts(agent))

    def _launch_parts(self, agent: dict[str, Any]) -> list[str]:
        codex_executable, codex_bin_dir = _resolve_codex_launcher()
        parts: list[str] = ["env"]
        for name in _session_env_unset_names():
            parts.extend(["-u", shlex.quote(name)])
        for name, value in _codex_launch_env_passthrough().items():
            parts.append(f"{shlex.quote(name)}={shlex.quote(value)}")
        if codex_bin_dir:
            parts.append(f"PATH={shlex.quote(codex_bin_dir)}:$PATH")
        parts.extend(
            [shlex.quote(codex_executable), "--disable", "tui_app_server", "--no-alt-screen", "--cd", shlex.quote(agent["cwd"])]
        )
        mode = self._effective_mode(agent)
        thinking = agent.get("desired_thinking") or agent.get("thinking") or "xhigh"
        fast = _agent_fast_enabled(agent.get("desired_fast"), agent.get("fast"))
        add_dirs: list[str] = []
        if mode in {"full-auto", "workspace-write"}:
            add_dirs.append(ensure_scratchpad_dir(agent))
            add_dirs.extend(_pending_add_dirs(agent))
        if mode == "yolo":
            parts.append("--dangerously-bypass-approvals-and-sandbox")
        elif mode == "full-auto":
            parts.append("--full-auto")
        elif mode == "workspace-write":
            parts.extend(["-a", "on-request", "-s", "workspace-write"])
        elif mode == "danger-full-access":
            parts.extend(["-a", "never", "-s", "danger-full-access"])
        for add_dir in _unique_dirs(add_dirs):
            parts.extend(["--add-dir", shlex.quote(add_dir)])
        parts.extend(["-c", shlex.quote("trust_level=trusted")])
        parts.extend(["-c", shlex.quote(f"model_reasoning_effort={thinking}")])
        if fast:
            parts.extend(["-c", shlex.quote("service_tier=fast")])
            parts.extend(["-c", shlex.quote("features.fast_mode=true")])
        else:
            parts.extend(["-c", shlex.quote("features.fast_mode=false")])
        parts.extend(["-c", shlex.quote("check_for_update_on_startup=false")])
        return parts

    def _effective_mode(self, agent: dict[str, Any]) -> str:
        mode = agent.get("desired_mode") or agent.get("mode") or "yolo"
        # Legacy snapshots may still carry read-only; scratchpads require write access.
        return "workspace-write" if mode == "read-only" else str(mode)

    def _run_tmux(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(["tmux", *args], capture_output=True, text=True)
        if check and result.returncode != 0:
            stderr = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"tmux {' '.join(args)} failed: {stderr}")
        return result

    def _capture_pane_text(self, target: str, start_line: int = -80) -> str:
        capture = self._run_tmux(["capture-pane", "-pt", target, "-S", str(start_line)])
        return capture.stdout or ""

    def _pane_current_command(self, target: str) -> str:
        current = self._run_tmux(["display-message", "-p", "-t", target, "#{pane_current_command}"])
        return (current.stdout or "").strip()

    def _new_session_command(self, session: str, cwd: str, shell: str) -> list[str]:
        command = ["new-session", "-d", "-s", session, "-c", cwd, "env"]
        for name in _session_env_unset_names():
            command.extend(["-u", name])
        for name, value in _session_env_passthrough().items():
            command.append(f"{name}={value}")
        command.extend([shell, "-l"])
        return command

    def _sanitize_tmux_session_environment(self, session: str) -> None:
        for name in _session_env_unset_names():
            self._run_tmux(["set-environment", "-t", session, "-u", name], check=False)
        desired = _session_env_passthrough()
        managed_names = {"FLOW_HOME", "CODEX_HOME", "HOME", "PATH", "VIRTUAL_ENV"}
        for name in managed_names:
            if name not in desired:
                self._run_tmux(["set-environment", "-t", session, "-u", name], check=False)
        for name, value in desired.items():
            self._run_tmux(["set-environment", "-t", session, name, value], check=False)

    def _wait_for_session(self, session: str, timeout_seconds: float = 2.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if subprocess.run(["tmux", "has-session", "-t", session], capture_output=True, text=True).returncode == 0:
                return
            time.sleep(0.05)
        raise RuntimeError(f"tmux session '{session}' did not stay alive after creation")

    def _wait_for_codex_ready(self, session: str, timeout_seconds: float = 120.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        target = f"{session}:0.0"
        last_trust_confirm_at = 0.0
        last_text = ""
        last_current_command = ""
        stable_ready_count = 0
        previous_ready_snapshot = ""
        while True:
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
                last_text = text
                last_current_command = current_command
                launch_failure = _codex_launch_failure_reason(text, current_command=current_command)
                if launch_failure:
                    raise RuntimeError(
                        f"Codex failed to launch in tmux session '{session}' "
                        f"({launch_failure}; tail={_pane_tail_summary(text)})"
                    )
                if _looks_like_codex_trust_prompt(text, current_command=current_command):
                    now_ts = time.monotonic()
                    stable_ready_count = 0
                    previous_ready_snapshot = ""
                    if now_ts - last_trust_confirm_at >= 1.0:
                        self._run_tmux(["send-keys", "-t", target, "Enter"], check=False)
                        last_trust_confirm_at = now_ts
                    if now_ts >= deadline:
                        break
                    time.sleep(0.2)
                    continue
                if _looks_like_codex_prompt_ready(text, current_command=current_command):
                    snapshot = "\n".join(line.rstrip() for line in text.splitlines()[-8:])
                    if snapshot == previous_ready_snapshot:
                        stable_ready_count += 1
                    else:
                        previous_ready_snapshot = snapshot
                        stable_ready_count = 1
                    if stable_ready_count >= 2:
                        return
                else:
                    stable_ready_count = 0
                    previous_ready_snapshot = ""
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        raise RuntimeError(
            f"Codex did not become ready in tmux session '{session}' "
            f"(command={last_current_command or 'unknown'}; tail={_pane_tail_summary(last_text)})"
        )

    def _wait_for_prompt_ready(self, session: str, timeout_seconds: float = 15.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        target = f"{session}:0.0"
        stable_ready_count = 0
        previous_ready_snapshot = ""
        while True:
            text = self._capture_pane_text(target)
            current_command = self._pane_current_command(target)
            if _looks_like_codex_prompt_ready(text, current_command=current_command):
                snapshot = "\n".join(line.rstrip() for line in text.splitlines()[-8:])
                if snapshot == previous_ready_snapshot:
                    stable_ready_count += 1
                else:
                    previous_ready_snapshot = snapshot
                    stable_ready_count = 1
                if stable_ready_count >= 2:
                    return
            else:
                stable_ready_count = 0
                previous_ready_snapshot = ""
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        raise RuntimeError(f"Codex prompt input did not become ready in tmux session '{session}'")

    def _ensure_codex_prompt_ready(self, agent: dict[str, Any], timeout_seconds: float = 120.0) -> None:
        session = agent["tmux_session"]
        if not self._session_has_live_codex(session):
            self._launch_codex(agent)
            self._wait_for_codex_ready(session, timeout_seconds=timeout_seconds)
            return
        try:
            self._wait_for_prompt_ready(session)
        except RuntimeError:
            if self._session_has_live_codex(session):
                raise
            self._launch_codex(agent)
            self._wait_for_codex_ready(session, timeout_seconds=timeout_seconds)

    def _clear_prompt_input(self, session: str, timeout_seconds: float = 5.0) -> None:
        target = f"{session}:0.0"
        text = self._capture_pane_text(target)
        current_command = self._pane_current_command(target)
        if not _is_codex_process_name(current_command):
            self._wait_for_prompt_ready(session, timeout_seconds=timeout_seconds)
            return

        content = _visible_prompt_content(text)
        if content is None or not content.strip():
            return
        if _is_codex_placeholder_prompt_content(content):
            # Codex can leave starter-template text in the active composer after
            # a turn. Clear just that line before pasting Flow's prompt; Enter
            # can otherwise submit the template instead of the pasted prompt.
            self._run_tmux(["send-keys", "-t", target, "C-u"], check=False)
            self._wait_for_prompt_ready(session, timeout_seconds=timeout_seconds)
            return

        # Ctrl-C clears Codex's whole draft composer when text is present. This
        # is safer than Ctrl-U, which only clears one logical line in multiline
        # prompts and can leave stale flow-control text behind. Do not send it
        # on a blank composer: Codex treats that as an exit request.
        self._run_tmux(["send-keys", "-t", target, "C-c"], check=False)
        self._wait_for_prompt_ready(session, timeout_seconds=timeout_seconds)

    def _wait_for_paste_settle(self, target: str, baseline: str, timeout_seconds: float = 5.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        saw_change = False
        stable_text = ""
        stable_count = 0
        while time.monotonic() < deadline:
            text = self._capture_pane_text(target)
            if text != baseline:
                saw_change = True
                if text == stable_text:
                    stable_count += 1
                else:
                    stable_text = text
                    stable_count = 1
                if stable_count >= 2:
                    return
            time.sleep(0.05)
        if not saw_change:
            raise RuntimeError("pasted prompt never appeared in the Codex pane")

    def _paste_prompt(self, target: str, buffer_path: str, baseline: str) -> None:
        self._run_tmux(["load-buffer", buffer_path])
        self._paste_loaded_buffer(target)
        self._wait_for_paste_settle(target, baseline)

    def _paste_text(self, target: str, text: str, *, baseline: str | None = None, bracketed: bool = True) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            handle.write(text)
            buffer_path = handle.name
        try:
            self._run_tmux(["load-buffer", buffer_path])
            self._paste_loaded_buffer(target, bracketed=bracketed)
            if baseline is not None:
                self._wait_for_paste_settle(target, baseline)
        finally:
            try:
                os.unlink(buffer_path)
            except FileNotFoundError:
                pass

    def _paste_loaded_buffer(self, target: str, *, bracketed: bool = True) -> None:
        # Bracketed raw paste keeps multiline prompts as one paste event inside
        # Codex. Shell launch commands must avoid bracket control codes because
        # some shells/readline configurations accept the visible bytes literally
        # and corrupt the command as "00~...".
        args = ["paste-buffer", "-d", "-r", "-t", target]
        if bracketed:
            args.insert(2, "-p")
        self._run_tmux(args)

    def _submit_prompt(self, target: str) -> None:
        self._run_tmux(["send-keys", "-t", target, "Enter"])

    def _wait_for_thread_rename_event(
        self,
        agent: dict[str, Any],
        name: str,
        *,
        started_after: datetime,
        timeout_seconds: float = 10.0,
    ) -> bool:
        rollout_path = str(agent.get("rollout_path") or "")
        thread_id = str(agent.get("thread_id") or "")
        launch_marker = str(agent.get("launch_marker") or "")
        resolved_path, resolved_thread_id = self._resolve_rollout(thread_id, rollout_path, launch_marker, "")
        if not resolved_path:
            return False

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if _thread_name_updated_seen(
                _read_rollout_events(Path(resolved_path)),
                expected_name=name,
                thread_id=resolved_thread_id,
                started_after=started_after,
            ):
                return True
            time.sleep(0.1)
        return False

    def _wait_for_turn_start(
        self,
        agent: dict[str, Any],
        *,
        started_after: datetime,
        timeout_seconds: float = 10.0,
        request_id: str = "",
        prompt_text: str = "",
    ) -> TurnObservation:
        deadline = time.monotonic() + timeout_seconds
        thread_id = agent.get("thread_id", "") or ""
        rollout_path = agent.get("rollout_path", "") or ""
        launch_marker = agent.get("launch_marker", "") or ""
        target = f"{agent['tmux_session']}:0.0"
        submit_attempts = 0
        last_submit_attempt = time.monotonic()

        while time.monotonic() < deadline:
            resolved_path, resolved_thread_id = self._resolve_rollout(
                thread_id,
                rollout_path,
                launch_marker,
                format_utc(started_after),
            )
            if resolved_path:
                thread_id = resolved_thread_id or thread_id
                rollout_path = resolved_path
                events = _read_rollout_events(Path(resolved_path))
                turn = _find_turn(
                    events=events,
                    current_turn_id="",
                    current_request_id=request_id,
                    started_after=started_after,
                )
                if turn is not None:
                    status = "completed" if turn["status"] == "completed" else turn["status"]
                    return TurnObservation(
                        status=status,
                        thread_id=thread_id,
                        rollout_path=rollout_path,
                        turn_id=turn["turn_id"],
                        started_at=turn["started_at"],
                        ended_at=turn["ended_at"],
                        output_text=turn["output_text"],
                        raw_output=turn["raw_output"],
                        last_event_at=turn["last_event_at"],
                        abort_reason=turn["abort_reason"],
                    )
                request_acknowledged_at = _request_acknowledged_at(
                    events,
                    request_id=request_id,
                    started_after=started_after,
                )
                if request_acknowledged_at:
                    return TurnObservation(
                        status="running",
                        thread_id=thread_id,
                        rollout_path=rollout_path,
                        started_at=request_acknowledged_at,
                        last_event_at=request_acknowledged_at,
                    )
            now = time.monotonic()
            if (
                request_id
                and submit_attempts < 2
                and now - last_submit_attempt >= _SUBMIT_RETRY_INTERVAL_SECONDS
            ):
                text = self._capture_pane_text(target)
                current_command = self._pane_current_command(target)
                if (
                    _looks_like_codex_prompt_ready(text, current_command=current_command)
                    and not _codex_working_seen(text)
                    and (
                        _current_prompt_block_contains_request_id(text, request_id)
                        or _current_prompt_block_contains_paste_placeholder(text, len(prompt_text))
                    )
                ):
                    self._submit_prompt(target)
                    submit_attempts += 1
                    last_submit_attempt = now
            time.sleep(0.1)
        text = self._capture_pane_text(target)
        current_command = self._pane_current_command(target)
        auth_failure = _codex_prompt_submission_failure_reason(text, current_command=current_command)
        if auth_failure:
            raise RuntimeError(auth_failure)
        permission_prompt = _codex_permission_prompt_reason(text, current_command=current_command)
        if permission_prompt:
            raise RuntimeError(permission_prompt)
        raise RuntimeError(f"prompt submission was not acknowledged in tmux session '{agent['tmux_session']}'")

    def _session_has_live_codex(self, session: str) -> bool:
        target = f"{session}:0.0"
        current = subprocess.run(
            ["tmux", "display-message", "-p", "-t", target, "#{pane_current_command}"],
            capture_output=True,
            text=True,
        )
        if current.returncode != 0:
            return False
        current_command = (current.stdout or "").strip()
        if not _is_codex_process_name(current_command):
            return False
        text = self._capture_pane_text(target)
        if _codex_session_finished_seen(text):
            return False
        return _codex_prompt_submission_failure_reason(text, current_command=current_command) is None

    def _permission_prompt_reason(self, agent: dict[str, Any]) -> str | None:
        session = str(agent.get("tmux_session") or "").strip()
        if not session:
            return None
        target = f"{session}:0.0"
        try:
            text = self._capture_pane_text(target)
            current_command = self._pane_current_command(target)
        except RuntimeError:
            return None
        return _codex_permission_prompt_reason(text, current_command=current_command)

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
            if _rollout_contains_launch_marker(path, launch_marker):
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


def _rollout_contains_launch_marker(path: Path, launch_marker: str) -> bool:
    if not launch_marker:
        return False
    for event in _read_rollout_events(path):
        payload = event.get("payload") or {}
        event_type = event.get("type")
        if event_type == "event_msg" and payload.get("type") == "user_message":
            if launch_marker in str(payload.get("message") or ""):
                return True
            continue
        if event_type == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
            content = payload.get("content") or []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "input_text":
                    if launch_marker in str(item.get("text") or ""):
                        return True
    return False


def _attach_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("TMUX", None)
    return env


def _is_codex_process_name(value: str) -> bool:
    text = value.strip().lower()
    return text.startswith("codex") or text in {"node", "nodejs"}


def _format_agent_args(text: str) -> str:
    try:
        payload = json.loads(text or "{}")
    except Exception:
        return ""
    if not isinstance(payload, dict) or not payload:
        return ""
    return " ".join(f"{key}={value}" for key, value in sorted(payload.items()))


def _pending_add_dirs(agent: dict[str, Any]) -> list[str]:
    add_dirs = pending_state_payload(agent).get("add_dirs")
    if not isinstance(add_dirs, list):
        return []
    items: list[str] = []
    for item in add_dirs:
        text = str(item or "").strip()
        if text:
            items.append(text)
    return items


def _unique_dirs(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


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


def _thread_name_updated_seen(
    events: list[dict[str, Any]],
    *,
    expected_name: str,
    thread_id: str,
    started_after: datetime,
) -> bool:
    for event in events:
        payload = event.get("payload") or {}
        if event.get("type") != "event_msg" or payload.get("type") != "thread_name_updated":
            continue
        timestamp = parse_utc(event.get("timestamp") or "")
        if timestamp is not None and timestamp < started_after:
            continue
        event_thread_id = str(payload.get("thread_id") or "")
        event_name = str(payload.get("thread_name") or "")
        if thread_id and event_thread_id and event_thread_id != thread_id:
            continue
        if event_name == expected_name:
            return True
    return False


def _looks_like_codex_trust_prompt(text: str, *, current_command: str = "") -> bool:
    if not _is_codex_process_name(current_command):
        return False
    tail_lines = [line.strip().lower() for line in text.splitlines()[-20:] if line.strip()]
    if not tail_lines:
        return False
    tail = "\n".join(tail_lines)
    has_continue_prompt = any(line == "› 1. yes, continue" or line == "1. yes, continue" for line in tail_lines)
    return (
        has_continue_prompt
        and (
            "2. no, quit" in tail
            or "press enter to continue" in tail
            or "do you trust the contents of this directory?" in tail
        )
    )


def _find_turn(
    events: list[dict[str, Any]],
    *,
    current_turn_id: str,
    current_request_id: str,
    started_after: str | datetime,
) -> dict[str, str] | None:
    if isinstance(started_after, datetime):
        started_after_dt = started_after
    else:
        started_after_dt = parse_utc(started_after)
    turns: list[dict[str, str]] = []
    candidate: dict[str, str] | None = None
    bucket: list[dict[str, Any]] = []
    pending_request_id = ""

    def finalize_bucket() -> None:
        nonlocal candidate, bucket
        if not bucket or candidate is None:
            bucket = []
            candidate = None
            return

        assistant_messages: list[str] = []
        for bucket_event in bucket:
            payload = bucket_event.get("payload") or {}
            if not candidate.get("request_id"):
                request_id = _event_request_id(bucket_event)
                if request_id:
                    candidate["request_id"] = request_id
            if (
                bucket_event.get("type") == "response_item"
                and payload.get("type") == "message"
                and payload.get("role") == "assistant"
            ):
                text = _assistant_text(payload)
                if text:
                    assistant_messages.append(text)
                    candidate["raw_output"] = text
            elif bucket_event.get("type") == "event_msg" and payload.get("type") == "task_complete":
                candidate["status"] = "completed"
                candidate["ended_at"] = bucket_event.get("timestamp") or ""
                last_message = payload.get("last_agent_message")
                if isinstance(last_message, str) and last_message.strip():
                    candidate["output_text"] = last_message.strip()
            elif bucket_event.get("type") == "event_msg" and payload.get("type") == "turn_aborted":
                candidate["status"] = "aborted"
                candidate["ended_at"] = bucket_event.get("timestamp") or ""
                abort_reason = str(payload.get("reason") or "").strip()
                if abort_reason:
                    candidate["abort_reason"] = abort_reason
        if not candidate["output_text"] and assistant_messages:
            candidate["output_text"] = assistant_messages[-1]
        turns.append(candidate)
        bucket = []
        candidate = None

    for event in events:
        payload = event.get("payload") or {}
        if event.get("type") == "event_msg" and payload.get("type") == "task_started":
            finalize_bucket()
            turn_id = str(payload.get("turn_id") or "")
            if current_turn_id and turn_id != current_turn_id:
                continue
            bucket = [event]
            candidate = {
                "turn_id": turn_id,
                "started_at": event.get("timestamp") or "",
                "ended_at": "",
                "status": "running",
                "output_text": "",
                "raw_output": "",
                "last_event_at": event.get("timestamp") or "",
                "request_id": pending_request_id,
                "abort_reason": "",
            }
            pending_request_id = ""
            continue
        if bucket:
            bucket.append(event)
            if candidate is not None:
                candidate["last_event_at"] = event.get("timestamp") or candidate["last_event_at"]
            continue
        request_id = _event_request_id(event)
        if request_id:
            pending_request_id = request_id

    finalize_bucket()

    if current_turn_id:
        matching_turns = [turn for turn in turns if turn.get("turn_id") == current_turn_id]
        turn = matching_turns[-1] if matching_turns else None
        terminal = _terminal_turn_event(events, current_turn_id)
        if terminal is not None:
            payload = terminal.get("payload") or {}
            if turn is None:
                started = _task_started_event(events, current_turn_id)
                turn = {
                    "turn_id": current_turn_id,
                    "started_at": (started or {}).get("timestamp") or "",
                    "ended_at": "",
                    "status": "running",
                    "output_text": "",
                    "raw_output": "",
                    "last_event_at": (started or terminal).get("timestamp") or "",
                    "request_id": current_request_id,
                    "abort_reason": "",
                }
            turn["last_event_at"] = terminal.get("timestamp") or turn["last_event_at"]
            turn["ended_at"] = terminal.get("timestamp") or ""
            if payload.get("type") == "task_complete":
                turn["status"] = "completed"
                last_message = payload.get("last_agent_message")
                if isinstance(last_message, str) and last_message.strip():
                    turn["output_text"] = last_message.strip()
            elif payload.get("type") == "turn_aborted":
                turn["status"] = "aborted"
                abort_reason = str(payload.get("reason") or "").strip()
                if abort_reason:
                    turn["abort_reason"] = abort_reason
        return turn
    if not turns:
        return None
    if current_request_id:
        matching_turns = [turn for turn in turns if turn.get("request_id") == current_request_id]
        return matching_turns[-1] if matching_turns else None
    if started_after_dt is None:
        return turns[-1]

    matching_turns: list[dict[str, str]] = []
    for turn in turns:
        turn_started_at = parse_utc(turn["started_at"])
        turn_last_event_at = parse_utc(turn["last_event_at"])
        if turn_started_at is not None and turn_started_at >= started_after_dt:
            matching_turns.append(turn)
            continue
        if turn_last_event_at is not None and turn_last_event_at >= started_after_dt:
            matching_turns.append(turn)
    return matching_turns[-1] if matching_turns else None


def _task_started_event(events: list[dict[str, Any]], turn_id: str) -> dict[str, Any] | None:
    for event in events:
        payload = event.get("payload") or {}
        if event.get("type") == "event_msg" and payload.get("type") == "task_started" and payload.get("turn_id") == turn_id:
            return event
    return None


def _terminal_turn_event(events: list[dict[str, Any]], turn_id: str) -> dict[str, Any] | None:
    terminal: dict[str, Any] | None = None
    for event in events:
        payload = event.get("payload") or {}
        if (
            event.get("type") == "event_msg"
            and payload.get("type") in {"task_complete", "turn_aborted"}
            and payload.get("turn_id") == turn_id
        ):
            terminal = event
    return terminal


def _assistant_text(payload: dict[str, Any]) -> str:
    content = payload.get("content") or []
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "output_text" and item.get("text"):
            parts.append(str(item["text"]))
    return "\n".join(part.strip() for part in parts if str(part).strip()).strip()


def _request_acknowledged_at(
    events: list[dict[str, Any]],
    *,
    request_id: str,
    started_after: str | datetime,
) -> str:
    if not request_id:
        return ""
    if isinstance(started_after, datetime):
        started_after_dt = started_after
    else:
        started_after_dt = parse_utc(started_after)
    for event in events:
        timestamp = parse_utc(event.get("timestamp") or "")
        if started_after_dt is not None and timestamp is not None and timestamp < started_after_dt:
            continue
        if _event_request_id(event) == request_id:
            return event.get("timestamp") or ""
    return ""


def _event_request_id(event: dict[str, Any]) -> str:
    payload = event.get("payload") or {}
    event_type = event.get("type")
    if event_type == "event_msg" and payload.get("type") == "user_message":
        return _extract_request_id(str(payload.get("message") or ""))
    if event_type == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
        content = payload.get("content") or []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "input_text":
                request_id = _extract_request_id(str(item.get("text") or ""))
                if request_id:
                    return request_id
    return ""


def _extract_request_id(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("request_id:"):
            return stripped.split(":", 1)[1].strip()
    return ""


_ALWAYS_UNSET_SESSION_ENV = {
    "CHATGPT_DESKTOP_THREAD_ID",
    "CODEX_CI",
    "CODEX_HOME",
    "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",
    "CODEX_SANDBOX",
    "CODEX_SHELL",
    "CODEX_THREAD_ID",
    "FLOW_HOME",
    "VIRTUAL_ENV",
    "__CFBundleIdentifier",
}


def _session_env_unset_names() -> list[str]:
    names: set[str] = set()
    for key in os.environ:
        if key.startswith("CHATGPT_"):
            names.add(key)
        elif key.startswith("CODEX_"):
            names.add(key)
    names.update(_ALWAYS_UNSET_SESSION_ENV)
    return sorted(names)


def _session_env_passthrough() -> dict[str, str]:
    names = ("FLOW_HOME", "CODEX_HOME", "HOME", "PATH", "VIRTUAL_ENV")
    result: dict[str, str] = {}
    for name in names:
        value = os.environ.get(name)
        if value:
            result[name] = value
    return result


def _codex_launch_env_passthrough() -> dict[str, str]:
    values = _session_env_passthrough()
    values.pop("PATH", None)
    return values


def _real_user_home() -> Path | None:
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:
        return None


def _resolve_codex_launcher() -> tuple[str, str]:
    explicit = shutil.which("codex")
    if explicit:
        resolved = Path(explicit).resolve()
        return str(resolved), str(resolved.parent)

    real_home = _real_user_home()
    if real_home:
        version_root = real_home / ".nvm" / "versions" / "node"
        candidates = sorted(version_root.glob("*/bin"), reverse=True)
        for candidate in candidates:
            codex_path = candidate / "codex"
            node_path = candidate / "node"
            if codex_path.is_file() and os.access(codex_path, os.X_OK) and node_path.is_file() and os.access(node_path, os.X_OK):
                return str(codex_path), str(candidate)

    return "codex", ""


def _codex_launch_failure_reason(text: str, *, current_command: str = "") -> str | None:
    command = current_command.strip().lower()
    if command and command not in {"bash", "sh", "zsh", "fish"}:
        return None

    normalized_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not normalized_lines:
        return None

    for line in normalized_lines[-12:]:
        lowered = line.lower()
        if "codex" not in lowered:
            continue
        if "command not found" in lowered:
            return line
        if "no such file or directory" in lowered:
            return line
        if "not recognized as an internal or external command" in lowered:
            return line
    return None


def _codex_prompt_submission_failure_reason(text: str, *, current_command: str = "") -> str | None:
    if not _is_codex_process_name(current_command):
        return None

    normalized_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not normalized_lines:
        return None
    tail = normalized_lines[-40:]
    lowered = "\n".join(line.lower() for line in tail)

    if "your access token could not be refreshed" in lowered and "log out and sign in again" in lowered:
        return "Codex authentication failed; run `codex logout` and `codex login`"
    if "provided authentication token is expired" in lowered:
        return "Codex authentication expired; run `codex logout` and `codex login`"
    return None


def _codex_session_finished_seen(text: str) -> bool:
    tail_lines = [line.strip().lower() for line in text.splitlines()[-16:] if line.strip()]
    return any("to continue this session, run codex resume" in line for line in tail_lines)


def _codex_working_seen(text: str) -> bool:
    tail = "\n".join(line.strip().lower() for line in text.splitlines()[-16:] if line.strip())
    return "working (" in tail and "esc to interrupt" in tail


def _looks_like_codex_tui_ready(text: str, *, current_command: str = "") -> bool:
    command = current_command.strip().lower()
    if "OpenAI Codex" in text and any(marker in text for marker in ("model:", "directory:", "gpt-5.4", "gpt-5")):
        return True

    # After startup or restore, Codex may already be in an active conversation
    # view where the initial banner has scrolled away. In that case the tmux
    # pane still belongs to the codex process and contains our conversation UI.
    if _is_codex_process_name(command):
        if "[flow-control]" in text:
            return True
        if any(marker in text for marker in ("› ", "• ", "Run /", "gpt-5.4", "gpt-5", "model:", "directory:")):
            return True
    return False


def _looks_like_codex_prompt_ready(text: str, *, current_command: str = "") -> bool:
    if not _is_codex_process_name(current_command):
        return False
    if _codex_working_seen(text):
        return False
    if _looks_like_codex_trust_prompt(text, current_command=current_command):
        return False
    if _codex_permission_prompt_reason(text, current_command=current_command):
        return False

    lines = [line.rstrip("\r") for line in text.splitlines()]
    tail = lines[-24:]
    nonempty_tail = [line.strip() for line in tail if line.strip()]
    if not nonempty_tail:
        return False
    if not any("gpt-" in line.lower() or "model:" in line.lower() for line in nonempty_tail):
        return False

    for raw_line in reversed(tail):
        line = raw_line.lstrip()
        if not line.startswith("›"):
            continue
        if line == "›":
            return True
        if not line.startswith("› "):
            continue
        content = line[2:].strip()
        if not content:
            return True
        if content.startswith("/rename"):
            continue
        if content.startswith("[flow "):
            continue
        if content.startswith("[flow-control]"):
            continue
        return True
    return False


def _codex_permission_prompt_reason(text: str, *, current_command: str = "") -> str | None:
    if not _is_codex_process_name(current_command):
        return None

    tail_lines = [line.strip() for line in text.splitlines()[-32:] if line.strip()]
    if not tail_lines:
        return None

    last_prompt_index = -1
    for index, line in enumerate(tail_lines):
        if line.startswith("›"):
            last_prompt_index = index

    if last_prompt_index >= 0:
        content = tail_lines[last_prompt_index].lstrip("›").strip().lower()
        if content and not _looks_like_codex_menu_choice(content):
            return None
        tail_lines = tail_lines[max(0, last_prompt_index - 12) :]

    tail = "\n".join(line.lower() for line in tail_lines)
    if not any(
        marker in tail
        for marker in (
            "needs your approval",
            "do you want to approve network access",
            "tool call needs your approval",
            "allow codex to run",
            "requires approval by policy",
            "requires approval:",
            "would you like to run the following command?",
            "would you like to run this command?",
            "press enter to confirm or esc to cancel",
            "yes, proceed",
            "yes, and don't ask again",
        )
    ):
        return None
    return "Codex is waiting for permissions approval"


def _looks_like_codex_menu_choice(content: str) -> bool:
    lowered = content.strip().lower()
    if not lowered:
        return True
    if lowered[0].isdigit():
        return True
    return lowered.startswith(("yes", "no", "allow", "approve", "deny", "cancel", "go back"))


def _visible_prompt_content(text: str) -> str | None:
    lines = [line.rstrip("\r") for line in text.splitlines()]
    tail = lines[-24:]
    for raw_line in reversed(tail):
        line = raw_line.lstrip()
        if line == "›":
            return ""
        if line.startswith("› "):
            return line[2:]
    return None


def _current_prompt_block(text: str) -> str:
    lines = [line.rstrip("\r") for line in text.splitlines()]
    start = None
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].lstrip().startswith("›"):
            start = index
            break
    if start is None:
        return ""
    block: list[str] = []
    for offset, raw_line in enumerate(lines[start:]):
        line = raw_line.lstrip()
        if offset == 0 and line.startswith("›"):
            block.append(line[1:].lstrip())
        else:
            block.append(raw_line)
    return "\n".join(block)


def _current_prompt_block_contains_request_id(text: str, request_id: str) -> bool:
    if not request_id:
        return False
    return request_id in _current_prompt_block(text)


def _current_prompt_block_contains_paste_placeholder(text: str, char_count: int) -> bool:
    if char_count <= 0:
        return False
    return f"[Pasted Content {char_count} chars]" in _current_prompt_block(text)


_CODEX_PLACEHOLDER_PROMPTS = {
    "Explain this codebase",
    "Summarize recent commits",
    "Implement {feature}",
    "Implement {{feature}}",
    "Find and fix a bug in @filename",
    "Write tests for @filename",
    "Improve documentation in @filename",
    "Run /review on my current changes",
    "Use /skills to list available skills",
}


def _is_codex_placeholder_prompt_content(content: str) -> bool:
    return content.strip() in _CODEX_PLACEHOLDER_PROMPTS


def _sanitize_thread_name(name: str) -> str:
    return " ".join(str(name).replace("\x00", "").split())


def _agent_fast_enabled(desired_fast: Any, current_fast: Any) -> bool:
    if desired_fast is not None and str(desired_fast).strip() != "":
        return _coerce_bool(desired_fast)
    return _coerce_bool(current_fast)


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return bool(value)


def _pane_tail_summary(text: str, *, lines: int = 6) -> str:
    tail = [line.strip() for line in text.splitlines()[-lines:] if line.strip()]
    return " | ".join(tail)
