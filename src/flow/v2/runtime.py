"""Single-run foreground Flow 2.0 state engine."""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psutil

from flow.common import parse_wait_seconds

from .backend import AgentBackend, BackendTurn, CodexBackend
from .constants import (
    EX_NEEDS_HELP,
    EX_RUNTIME,
    EX_SIGINT,
    EX_SIGTERM,
    IMPLICIT_FINISH,
    IMPLICIT_KEEP_WORKING,
    IMPLICIT_NEEDS_HELP,
)
from .output import Reporter
from .prompts import DECISION_SCHEMA, Decision, continue_prompt, decision_prompt, parse_decision, state_prompt
from .scratchpad import mark_process, repair_scratchpad, utc_now
from .spec import FlowSpec, StateSpec


class FlowRuntime:
    def __init__(
        self,
        flow: FlowSpec,
        scratchpad: str | Path,
        metadata: dict[str, Any],
        *,
        backend: AgentBackend | None = None,
        reporter: Reporter | None = None,
    ) -> None:
        self.flow = flow
        self.scratchpad = str(Path(scratchpad).expanduser().resolve())
        self.metadata = metadata
        self.backend = backend or CodexBackend()
        self.reporter = reporter or Reporter(json_output=bool(metadata.get("json")))
        self.resuming = bool(metadata.get("thread"))
        self.stop_signal: int | None = None
        self.stop_event = asyncio.Event()
        self._installed_signals: list[int] = []
        self._managed_pids: set[int] = set()

    async def run(self) -> int:
        mark_process(self.metadata)
        self._checkpoint(last_error="")
        self._install_signal_handlers()
        try:
            thread = await self.backend.open(
                self.flow,
                str(self.metadata.get("thread") or ""),
                self.scratchpad,
            )
            self._checkpoint(thread=thread)
            self.reporter.emit(
                "start",
                flow=self.flow.name,
                state=self.metadata["state"],
                phase=self.metadata["phase"],
                ready_at=self.metadata.get("ready_at", ""),
                scratchpad=self.scratchpad,
                thread=thread,
                resumed=self.resuming,
            )
            recovered_exit = await self._recover_active_turn()
            if recovered_exit is not None:
                return recovered_exit
            return await self._run_states()
        except Exception as exc:
            if self.stop_signal is not None:
                return self._finish_interrupted()
            self._checkpoint(
                status="error",
                last_error=str(exc),
                last_outcome="runtime-error",
                exit_code=EX_RUNTIME,
                ended_at=utc_now(),
                resumable=True,
            )
            self.reporter.emit("error", message=str(exc), state=self.metadata.get("state"))
            self._emit_final(EX_RUNTIME, resumable=True)
            return EX_RUNTIME
        finally:
            self._remove_signal_handlers()
            await self.backend.close()
            if self.stop_signal is not None:
                self._terminate_managed_processes()

    async def _run_states(self) -> int:
        while True:
            self._raise_if_stopping()
            state = self._state()
            phase = str(self.metadata["phase"])

            if phase == "enter_state":
                self.reporter.emit("state", state=state.name, terminal=state.terminal)
                if state.wait:
                    self._set_wait(state.wait, "state_wait")
                    continue
                self._checkpoint(phase="work_pending")
                continue

            if phase in {"state_wait", "transition_wait"}:
                await self._wait_until_ready(phase)
                continue

            if phase in {"work_pending", "work_interrupted"}:
                if not state.prompt.strip():
                    if not state.terminal:
                        raise RuntimeError(f"state '{state.name}' has no prompt")
                    return self._finish_terminal(state, "terminal state has no prompt")
                prompt = (
                    continue_prompt(self.flow, state, self.scratchpad)
                    if phase == "work_interrupted"
                    else state_prompt(self.flow, state, self.scratchpad)
                )
                turn = await self._run_turn(prompt, state, kind="work")
                self._require_completed(turn, state)
                self._raise_if_stopping()
                self._checkpoint(phase="evaluate", turn_id="", turn_kind="")
                continue

            if phase == "continue_pending":
                turn = await self._run_turn(
                    continue_prompt(self.flow, state, self.scratchpad),
                    state,
                    kind="continue",
                )
                self._require_completed(turn, state)
                self._raise_if_stopping()
                self._checkpoint(phase="evaluate", turn_id="", turn_kind="")
                continue

            if phase in {"evaluate", "decision_pending"}:
                prompt, request_id = decision_prompt(self.flow, state, self.scratchpad)
                self._checkpoint(phase="decision_pending", request_id=request_id)
                turn = await self._run_turn(
                    prompt,
                    state,
                    kind="decision",
                    output_schema=DECISION_SCHEMA,
                )
                self._require_completed(turn, state)
                self._raise_if_stopping()
                decision = parse_decision(turn.final_response, request_id)
                outcome = self._apply_decision(state, decision)
                if outcome is not None:
                    return outcome
                continue

            if phase == "completed":
                return int(self.metadata.get("exit_code") or 0)
            raise RuntimeError(f"cannot resume unknown phase '{phase}'")

    async def _run_turn(
        self,
        prompt: str,
        state: StateSpec,
        *,
        kind: str,
        output_schema: dict[str, Any] | None = None,
    ) -> BackendTurn:
        phase = "decision_turn" if kind == "decision" else ("continue_turn" if kind == "continue" else "work_turn")
        self._checkpoint(phase=phase, turn_kind=kind, turn_id="")

        def started(turn_id: str) -> None:
            self._checkpoint(turn_id=turn_id)

        return await self.backend.run_turn(
            prompt,
            state=state,
            output_schema=output_schema,
            on_activity=(
                None if kind == "decision" else lambda text: self.reporter.activity(text, state=state.name)
            ),
            on_started=started,
        )

    async def _recover_active_turn(self) -> int | None:
        phase = str(self.metadata.get("phase") or "")
        if phase not in {"work_turn", "continue_turn", "decision_turn"}:
            return None
        turn_id = str(self.metadata.get("turn_id") or "")
        if not turn_id:
            self._checkpoint(phase="evaluate" if phase == "decision_turn" else "work_interrupted")
            return None
        recovered = await self.backend.recover_turn(turn_id)
        if recovered is None or recovered.status == "interrupted":
            self._checkpoint(
                phase="evaluate" if phase == "decision_turn" else "work_interrupted",
                turn_id="",
                turn_kind="",
            )
            return None
        self._require_completed(recovered, self._state())
        if phase == "decision_turn":
            request_id = str(self.metadata.get("request_id") or "")
            decision = parse_decision(recovered.final_response, request_id)
            return self._apply_decision(self._state(), decision)
        else:
            self._checkpoint(phase="evaluate", turn_id="", turn_kind="")
        return None

    def _apply_decision(self, state: StateSpec, decision: Decision) -> int | None:
        choice = decision.choice.replace("_", "-")
        if choice == IMPLICIT_NEEDS_HELP:
            return self._finish_needs_help(decision.reason)
        if choice == IMPLICIT_KEEP_WORKING:
            self._checkpoint(
                phase="continue_pending",
                turn_id="",
                turn_kind="",
                request_id="",
                last_outcome=decision.reason,
            )
            return None
        if state.terminal:
            if choice != IMPLICIT_FINISH:
                raise RuntimeError(f"terminal state '{state.name}' returned invalid action '{decision.choice}'")
            return self._finish_terminal(state, decision.reason)

        transition = next((item for item in state.transitions if item.target == choice), None)
        if transition is None:
            raise RuntimeError(f"state '{state.name}' returned unknown transition '{decision.choice}'")
        self.reporter.emit(
            "transition",
            from_state=state.name,
            to_state=transition.target,
            reason=decision.reason,
            wait=transition.wait or "",
        )
        self._checkpoint(
            state=transition.target,
            phase="enter_state",
            turn_id="",
            turn_kind="",
            request_id="",
            ready_at="",
            last_outcome=decision.reason,
        )
        if transition.wait:
            self._set_wait(transition.wait, "transition_wait")
        return None

    def _finish_terminal(self, state: StateSpec, reason: str) -> int:
        exit_code = int(state.exit_code or 0)
        self._checkpoint(
            phase="completed",
            status="completed",
            ended_at=utc_now(),
            exit_code=exit_code,
            resumable=False,
            last_outcome=reason,
            last_error="",
            turn_id="",
            turn_kind="",
            request_id="",
            ready_at="",
        )
        self._emit_final(exit_code, resumable=False)
        return exit_code

    def _finish_needs_help(self, reason: str) -> int:
        self._checkpoint(
            phase="evaluate",
            status="needs-help",
            ended_at=utc_now(),
            exit_code=EX_NEEDS_HELP,
            resumable=True,
            last_outcome="needs-help",
            last_error=reason,
            turn_id="",
            turn_kind="",
            request_id="",
        )
        self.reporter.emit("needs_help", reason=reason, state=self.metadata["state"])
        thread = str(self.metadata.get("thread") or "")
        if thread:
            self.reporter.diagnostic(f"codex resume {shlex.quote(thread)}")
        self.reporter.diagnostic(f"flow resume {shlex.quote(self.scratchpad)}")
        self._emit_final(EX_NEEDS_HELP, resumable=True)
        return EX_NEEDS_HELP

    def request_stop(self, signum: int) -> None:
        if self.stop_signal is not None:
            os._exit(128 + signum)
        self.stop_signal = signum
        self._managed_pids = _descendant_pids()
        self.stop_event.set()
        try:
            asyncio.get_running_loop().create_task(self.backend.interrupt_active())
        except RuntimeError:
            pass

    def _raise_if_stopping(self) -> None:
        if self.stop_signal is not None:
            raise RuntimeError("flow interrupted")

    def _finish_interrupted(self) -> int:
        signum = int(self.stop_signal or signal.SIGINT)
        code = EX_SIGINT if signum == signal.SIGINT else EX_SIGTERM
        name = signal.Signals(signum).name
        phase = str(self.metadata.get("phase") or "")
        resume_phase = "evaluate" if phase == "decision_turn" else "work_interrupted" if phase in {
            "work_turn",
            "continue_turn",
        } else phase
        self._checkpoint(
            phase=resume_phase,
            status="interrupted",
            ended_at=utc_now(),
            exit_code=code,
            resumable=True,
            last_outcome="interrupted",
            last_error=name,
            turn_id="",
            turn_kind="",
        )
        self.reporter.emit("interrupted", signal=name, state=self.metadata["state"])
        self._emit_final(code, resumable=True)
        return code

    def _set_wait(self, duration: str, phase: str) -> None:
        ready = datetime.now(timezone.utc) + timedelta(seconds=parse_wait_seconds(duration))
        ready_at = ready.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        self._checkpoint(phase=phase, ready_at=ready_at)
        self.reporter.emit("waiting", state=self.metadata["state"], ready_at=ready_at, duration=duration)

    async def _wait_until_ready(self, phase: str) -> None:
        ready_at = _parse_utc(str(self.metadata.get("ready_at") or ""))
        if ready_at is None:
            raise RuntimeError(f"phase '{phase}' has no valid ready_at")
        seconds = max(0.0, (ready_at - datetime.now(timezone.utc)).total_seconds())
        if seconds:
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
            except asyncio.TimeoutError:
                pass
        self._raise_if_stopping()
        self._checkpoint(phase="enter_state" if phase == "transition_wait" else "work_pending", ready_at="")

    def _require_completed(self, turn: BackendTurn, state: StateSpec) -> None:
        if turn.status == "completed":
            return
        if turn.status == "interrupted" and self.stop_signal is not None:
            self._raise_if_stopping()
        detail = turn.error or turn.status
        raise RuntimeError(f"Codex turn failed in state '{state.name}': {detail}")

    def _state(self) -> StateSpec:
        name = str(self.metadata.get("state") or "")
        try:
            return self.flow.states[name]
        except KeyError as exc:
            raise RuntimeError(f"scratchpad references unknown state '{name}'") from exc

    def _checkpoint(self, **updates: Any) -> None:
        self.metadata.update(updates)
        repair_scratchpad(self.scratchpad, self.metadata)

    def _emit_final(self, exit_code: int, *, resumable: bool) -> None:
        self.reporter.emit(
            "final",
            flow=self.flow.name,
            state=self.metadata.get("state"),
            phase=self.metadata.get("phase"),
            exit_code=exit_code,
            scratchpad=self.scratchpad,
            thread=self.metadata.get("thread", ""),
            resumable=resumable,
            outcome=self.metadata.get("last_outcome", ""),
            error=self.metadata.get("last_error", ""),
        )

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, self.request_stop, signum)
                self._installed_signals.append(signum)
            except (NotImplementedError, RuntimeError):  # pragma: no cover - Windows/non-main thread
                pass

    def _remove_signal_handlers(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for signum in self._installed_signals:
            loop.remove_signal_handler(signum)
        self._installed_signals.clear()

    def _terminate_managed_processes(self) -> None:
        processes: list[psutil.Process] = []
        for pid in self._managed_pids:
            try:
                process = psutil.Process(pid)
                process.terminate()
                processes.append(process)
            except psutil.Error:
                continue
        _, alive = psutil.wait_procs(processes, timeout=2.0)
        for process in alive:
            try:
                process.kill()
            except psutil.Error:
                pass


def _parse_utc(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _descendant_pids() -> set[int]:
    try:
        return {process.pid for process in psutil.Process().children(recursive=True)}
    except (psutil.Error, OSError):
        return set()
