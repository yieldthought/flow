"""Background runtime loop."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import sys
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from .backend import AgentBackend, CodexBackend
from .common import (
    IMPLICIT_TRANSITION_FINISH,
    IMPLICIT_TRANSITION_KEEP_WORKING,
    IMPLICIT_TRANSITION_NEEDS_HELP,
    IMPLICIT_TRANSITION_WAIT_FOR_CHILD,
    RESERVED_STATE_NAMES,
    current_actor,
    format_utc,
    normalize_phase,
    normalize_implicit_transition_name,
    parse_wait_seconds,
    parse_utc,
    pending_state_payload,
    utc_now,
)
from .flowfile import FlowSpec, StateSpec, TransitionSpec, flow_from_dict
from .paths import agent_scratchpad_dir
from .scratchpad import remove_scratchpad_dir, scratchpad_path_text
from .store import (
    clear_daemon_status,
    close_open_state_run,
    connect,
    get_agent,
    get_flow_snapshot,
    is_locked_error,
    get_meta,
    init_db,
    list_agents,
    latest_open_state_run,
    mark_command_processed,
    open_state_run,
    pending_commands,
    record_agent_event,
    record_daemon_event,
    record_daemon_exit,
    record_transition,
    set_daemon_status,
    set_meta,
    transaction,
    update_agent,
)

POLL_INTERVAL_SECONDS = 0.5


@dataclass(frozen=True)
class Decision:
    choice: str
    reason: str
    raw_json: str
    request_id: str = ""
    child_ids: tuple[int, ...] = ()


class Runtime:
    def __init__(self, backend: AgentBackend | None = None) -> None:
        self.backend = backend or CodexBackend()
        self._running = True
        self._recovered = False

    def run_forever(self) -> int:
        conn = connect()
        init_db(conn)
        started_at = format_utc(utc_now())
        set_daemon_status(conn, os.getpid(), started_at=started_at, heartbeat_at=started_at)
        conn.commit()
        exit_code = 0
        exited_at = ""

        def stop_handler(_signum: int, _frame: Any) -> None:
            self._running = False

        signal.signal(signal.SIGTERM, stop_handler)
        signal.signal(signal.SIGINT, stop_handler)

        try:
            while self._running:
                try:
                    self.tick(conn)
                except sqlite3.OperationalError as exc:
                    if not is_locked_error(exc):
                        raise
                    conn.rollback()
                    time.sleep(POLL_INTERVAL_SECONDS)
                    continue
                time.sleep(POLL_INTERVAL_SECONDS)
        except Exception as exc:  # pragma: no cover - defensive daemon guard
            exit_code = 1
            exited_at = format_utc(utc_now())
            details = traceback.format_exc().rstrip()
            try:
                record_daemon_event(
                    conn,
                    level="error",
                    message=str(exc),
                    created_at=exited_at,
                    details_text=details,
                )
                record_daemon_exit(conn, kind="error", exited_at=exited_at, error_text=details)
                conn.commit()
            except sqlite3.OperationalError as log_exc:
                if not is_locked_error(log_exc):
                    raise
                conn.rollback()
            print(details, file=sys.stderr)
        finally:
            try:
                if exit_code == 0:
                    record_daemon_exit(conn, kind="clean", exited_at=exited_at or format_utc(utc_now()))
                clear_daemon_status(conn)
                conn.commit()
            except sqlite3.OperationalError as exit_exc:
                if not is_locked_error(exit_exc):
                    raise
                conn.rollback()
            conn.close()
        return exit_code

    def tick(self, conn: Any) -> None:
        set_daemon_status(conn, os.getpid(), heartbeat_at=format_utc(utc_now()))
        with transaction(conn):
            self._recover_agents_after_restart(conn)
        self._process_commands(conn)
        self._process_shutdown(conn)
        for row in list_agents(conn):
            agent = dict(row)
            try:
                self._tick_agent(conn, agent)
            except Exception as exc:  # pragma: no cover - defensive runtime guard
                details = traceback.format_exc().rstrip()
                self._enter_needs_help(conn, agent, str(exc))
                if str(exc) != str(agent.get("last_error") or ""):
                    record_daemon_event(
                        conn,
                        level="warning",
                        message=f"agent #{agent['id']} {agent['flow_name']}:{agent['current_state']} {exc}",
                        details_text=details,
                    )

    def _recover_agents_after_restart(self, conn: Any) -> None:
        if self._recovered:
            return
        for row in list_agents(conn):
            agent = dict(row)
            if agent["ended_at"]:
                continue
            phase = normalize_phase(agent["phase"])
            clear_turn_fields = {
                "current_turn_id": "",
                "current_turn_kind": "",
                "current_turn_started_at": "",
                "current_request_id": "",
            }
            fields = {"last_error": ""}
            if agent["substate"] == "normal":
                pending_kind = _pending_kind(agent)
                if agent["current_turn_started_at"] and phase in {"working", "submitting"}:
                    fields["phase"] = phase
                elif phase in {"waiting", "waiting_children"}:
                    fields.update(clear_turn_fields)
                    fields["phase"] = "waiting"
                    if phase == "waiting_children":
                        fields["phase"] = "waiting_children"
                elif pending_kind == "children_wake":
                    fields.update(clear_turn_fields)
                    fields["phase"] = "resume_state"
                elif phase == "enter_state" and not agent["thread_id"] and not agent["last_prompt_sent_at"]:
                    fields.update(clear_turn_fields)
                    fields["phase"] = "enter_state"
                else:
                    fields.update(clear_turn_fields)
                    fields["phase"] = "resume_state"
            else:
                fields.update(clear_turn_fields)
                fields["phase"] = "paused"
                close_open_state_run(conn, int(agent["id"]))
            update_agent(conn, int(agent["id"]), **fields)
        self._recovered = True

    def _process_commands(self, conn: Any) -> None:
        for row in pending_commands(conn):
            agent_row = get_agent(conn, int(row["agent_id"]))
            if agent_row is None:
                mark_command_processed(conn, int(row["id"]), "unknown agent")
                continue
            agent = dict(agent_row)
            payload = json.loads(row["payload_json"] or "{}")
            actor = str(row["actor"] or "").strip() or current_actor()
            source = str(row["source"] or "").strip()
            error = ""
            try:
                self._apply_command(conn, agent, row["kind"], payload, actor=actor, source=source)
            except Exception as exc:  # pragma: no cover - exercised by manual runs
                error = str(exc)
                details = traceback.format_exc().rstrip()
                if error != str(agent.get("last_error") or ""):
                    record_agent_event(
                        conn,
                        int(agent["id"]),
                        "error",
                        state_name=agent["current_state"],
                        reason=error,
                        payload={"command": row["kind"], "actor": actor, "source": source},
                    )
                    record_daemon_event(
                        conn,
                        level="warning",
                        message=f"command {row['kind']} failed for agent #{agent['id']}: {error}",
                        details_text=details,
                    )
                update_agent(conn, int(agent["id"]), last_error=error, status_message=error)
            mark_command_processed(conn, int(row["id"]), error)

    def _apply_command(
        self,
        conn: Any,
        agent: dict[str, Any],
        kind: str,
        payload: dict[str, Any],
        *,
        actor: str,
        source: str,
    ) -> None:
        agent_id = int(agent["id"])
        command_payload = {"actor": actor, "source": source} if source else {"actor": actor}
        if kind == "pause":
            close_open_state_run(conn, agent_id)
            reason = f"Paused by {actor}"
            record_agent_event(
                conn,
                agent_id,
                "pause",
                state_name=agent["current_state"],
                reason=reason,
                payload=command_payload,
            )
            update_agent(
                conn,
                agent_id,
                substate="interaction",
                phase="paused",
                status_message="Paused in interaction",
            )
            return
        if kind == "interrupt":
            if agent["phase"] == "finished":
                return
            self.backend.interrupt(agent)
            close_open_state_run(conn, agent_id)
            record_agent_event(
                conn,
                agent_id,
                "interrupt",
                state_name=agent["current_state"],
                reason=f"Interrupted by {actor}",
                payload=command_payload,
            )
            update_agent(
                conn,
                agent_id,
                substate="interaction",
                phase="paused",
                current_turn_id="",
                current_turn_kind="",
                current_turn_started_at="",
                current_request_id="",
            )
            return
        if kind == "resume":
            child_wait = _child_wait_state(conn, agent)
            remaining = _waiting_remaining_seconds(agent)
            should_resume = (
                agent.get("substate") != "normal"
                or child_wait is not None
                or bool(agent.get("ready_at"))
                or remaining > 0
            )
            if not should_resume:
                return
            fields = {"substate": "normal", "shutdown_mode": ""}
            if child_wait is not None:
                pending = [item["id"] for item in child_wait["pending"]]
                if pending:
                    fields["phase"] = "waiting_children"
                    fields["status_message"] = _waiting_children_status(pending)
                else:
                    self._queue_children_wake(conn, agent, child_wait)
                    fields = {}
            else:
                if remaining > 0:
                    fields["phase"] = "waiting"
                else:
                    if agent.get("ready_at"):
                        fields["ready_at"] = ""
                    fields["phase"] = "resume_state"
                    open_state_run(conn, agent_id, agent["current_state"])
            record_agent_event(
                conn,
                agent_id,
                "resume",
                state_name=agent["current_state"],
                reason=f"Resumed by {actor}",
                payload=command_payload,
            )
            if fields:
                update_agent(conn, agent_id, **fields)
            return
        if kind == "wake":
            if not agent.get("ready_at"):
                return
            reason = f"Woken by {actor}"
            record_agent_event(
                conn,
                agent_id,
                "wake",
                state_name=agent["current_state"],
                reason=reason,
                payload=command_payload,
            )
            fields = {"ready_at": "", "status_message": reason}
            if agent["substate"] == "normal":
                fields["phase"] = "resume_state"
                if latest_open_state_run(conn, agent_id) is None:
                    open_state_run(conn, agent_id, agent["current_state"])
            update_agent(conn, agent_id, **fields)
            return
        if kind == "move":
            target = str(payload["state"])
            flow = self._agent_flow(conn, agent)
            if target not in flow.states:
                raise ValueError(f"unknown state '{target}'")
            self.backend.interrupt(agent)
            update_agent(
                conn,
                agent_id,
                current_turn_id="",
                current_turn_kind="",
                current_turn_started_at="",
                current_request_id="",
            )
            reason = f"Moved to {target} by {actor}"
            record_agent_event(
                conn,
                agent_id,
                "decision",
                from_state=agent["current_state"],
                to_state=target,
                choice="move",
                reason=reason,
                payload=command_payload,
            )
            self._move_to_state(conn, agent, flow, target, reason, pause=True)
            return
        if kind == "stop":
            target = str(payload.get("state") or "stopped")
            if target != "stopped":
                flow = self._agent_flow(conn, agent)
                target_state = flow.states.get(target)
                if target_state is None or not target_state.end:
                    raise ValueError(f"stop target '{target}' is not an end state")
            record_agent_event(
                conn,
                agent_id,
                "decision",
                from_state=agent["current_state"],
                to_state=target,
                choice="stop",
                reason=f"Stopped by {actor}",
                payload=command_payload,
            )
            self._transition_terminal(conn, agent, target, choice="stop", reason=f"Stopped by {actor}")
            return
        if kind == "delete":
            self.backend.terminate(agent, immediate=True)
            remove_scratchpad_dir(agent_id)
            conn.execute("DELETE FROM agents WHERE id=?", (agent_id,))
            return
        raise ValueError(f"unsupported command '{kind}'")

    def _process_shutdown(self, conn: Any) -> None:
        mode = get_meta(conn, "shutdown_mode")
        if not mode:
            return
        flow_name = get_meta(conn, "shutdown_flow")
        targeted = [dict(row) for row in list_agents(conn, flow_name or None) if not row["ended_at"]]
        if mode == "now":
            for agent in targeted:
                self.backend.terminate(agent, immediate=True)
                update_agent(
                    conn,
                    int(agent["id"]),
                    current_turn_id="",
                    current_turn_kind="",
                    current_turn_started_at="",
                    current_request_id="",
                    phase="suspended",
                    shutdown_mode="",
                )
                close_open_state_run(conn, int(agent["id"]))
            self._clear_shutdown(conn)
            if not flow_name:
                self._running = False
            return

        all_stopped = True
        for agent in targeted:
            if agent["current_turn_started_at"]:
                all_stopped = False
                update_agent(conn, int(agent["id"]), shutdown_mode="graceful")
                continue
            self.backend.terminate(agent, immediate=False)
            update_agent(
                conn,
                int(agent["id"]),
                shutdown_mode="",
                phase="suspended",
            )
            close_open_state_run(conn, int(agent["id"]))
        if all_stopped:
            self._clear_shutdown(conn)
            if not flow_name:
                self._running = False

    def _clear_shutdown(self, conn: Any) -> None:
        set_meta(conn, "shutdown_mode", "")
        set_meta(conn, "shutdown_flow", "")
        set_meta(conn, "shutdown_requested_at", "")

    def _tick_agent(self, conn: Any, agent: dict[str, Any]) -> None:
        agent["phase"] = normalize_phase(agent["phase"])
        if agent["ended_at"] or agent["phase"] == "suspended":
            return
        if agent["substate"] in {"interaction", "needs_help"}:
            self._tick_paused_agent(conn, agent)
            return

        flow = self._agent_flow(conn, agent)
        state = flow.states.get(agent["current_state"])
        if state is None:
            update_agent(conn, int(agent["id"]), last_error=f"unknown current state '{agent['current_state']}'")
            return

        if self._handle_waiting_children(conn, agent, state):
            return

        if self._handle_delayed_entry(conn, agent, state):
            return

        agent["desired_mode"] = state.mode or flow.mode or agent["mode"]
        agent["desired_thinking"] = state.thinking or flow.thinking or agent["thinking"]
        agent["desired_fast"] = _state_fast_value(state.fast, flow.fast, agent.get("fast"))

        if state.end and not state.prompt and agent["phase"] in {"enter_state", "resume_state"}:
            self._transition_terminal(
                conn,
                agent,
                state.name,
                choice=IMPLICIT_TRANSITION_FINISH,
                reason="Reached terminal end state",
            )
            return

        auto_transition = _auto_transition(state)
        if auto_transition is not None and agent["phase"] in {"enter_state", "resume_state"}:
            reason = f"Automatically advancing via unconditional transition to {auto_transition.target}"
            record_transition(
                conn,
                int(agent["id"]),
                state.name,
                auto_transition.target,
                auto_transition.target,
                reason,
                json.dumps({"choice": auto_transition.target, "reason": reason}),
            )
            record_agent_event(
                conn,
                int(agent["id"]),
                "decision",
                from_state=state.name,
                to_state=auto_transition.target,
                choice=auto_transition.target,
                reason=reason,
            )
            self._move_to_state(conn, agent, flow, auto_transition.target, reason, transition=auto_transition)
            return

        ensure = self.backend.ensure_session(agent)
        if ensure:
            update_agent(conn, int(agent["id"]), **ensure)
            agent.update(ensure)

        phase = agent["phase"]
        if agent["current_turn_started_at"]:
            observation = self.backend.poll_turn(agent)
            self._apply_turn_observation_metadata(conn, agent, observation)
            if phase == "submitting":
                if observation.status == "pending":
                    update_agent(
                        conn,
                        int(agent["id"]),
                        status_message=f"Waiting for {agent['current_turn_kind'] or 'turn'} acknowledgement",
                    )
                    return
                fields = {
                    "phase": "working",
                    "status_message": f"Waiting for {agent['current_turn_kind'] or 'turn'}",
                }
                if observation.started_at:
                    fields["current_turn_started_at"] = observation.started_at
                    fields["last_prompt_sent_at"] = observation.started_at
                    agent["current_turn_started_at"] = observation.started_at
                    agent["last_prompt_sent_at"] = observation.started_at
                if observation.turn_id:
                    fields["current_turn_id"] = observation.turn_id
                    agent["current_turn_id"] = observation.turn_id
                update_agent(conn, int(agent["id"]), **fields)
                agent["phase"] = "working"
            if observation.status == "aborted":
                reason = observation.abort_reason or f"{agent['current_turn_kind'] or 'turn'} was aborted in Codex"
                close_open_state_run(conn, int(agent["id"]))
                record_agent_event(
                    conn,
                    int(agent["id"]),
                    "error",
                    state_name=state.name,
                    reason=reason,
                    payload={"turn_kind": agent.get("current_turn_kind") or "", "auto_retry": False},
                )
                update_agent(
                    conn,
                    int(agent["id"]),
                    substate="interaction",
                    current_turn_id="",
                    current_turn_kind="",
                    current_turn_started_at="",
                    current_request_id="",
                    phase="paused",
                    last_error=reason,
                    status_message="Paused after interrupted turn",
                )
                return
            if observation.status != "completed":
                return
            update_agent(conn, int(agent["id"]), current_turn_id=observation.turn_id, status_message="Turn completed")
            agent["current_turn_id"] = observation.turn_id
            self._maybe_set_thread_name(conn, agent, flow)
            try:
                self._handle_completed_turn(conn, agent, flow, state, observation.output_text or observation.raw_output)
            except Exception as exc:
                close_open_state_run(conn, int(agent["id"]))
                record_agent_event(conn, int(agent["id"]), "needs_help", state_name=state.name, reason=str(exc))
                update_agent(
                    conn,
                    int(agent["id"]),
                    substate="needs_help",
                    phase="paused",
                    last_error=str(exc),
                    status_message="Needs help",
                )
            return

        if agent["shutdown_mode"]:
            self.backend.terminate(agent, immediate=False)
            update_agent(conn, int(agent["id"]), shutdown_mode="", phase="resume_state")
            return
        if phase in {"enter_state", "resume_state"}:
            wake_prompt = _children_wake_prompt(flow, state, agent)
            if wake_prompt:
                request_id = _new_request_id()
                wake_prompt = _children_wake_prompt(flow, state, agent, request_id=request_id)
                self._send_turn(conn, agent, wake_prompt, "children_wake", request_id=request_id)
                return
            if state.prompt:
                request_id = _new_request_id()
                prompt = build_state_prompt(flow, state, agent, request_id=request_id)
                self._send_turn(
                    conn,
                    agent,
                    prompt,
                    "state_prompt" if phase == "enter_state" else "resume_prompt",
                    request_id=request_id,
                )
            else:
                request_id = _new_request_id()
                prompt = build_transition_prompt(flow, state, agent, allow_keep_working=False, request_id=request_id)
                self._send_turn(conn, agent, prompt, "transition_eval", request_id=request_id)
            return
        if phase == "continue_state":
            request_id = _new_request_id()
            prompt = build_continue_prompt(flow, state, agent, request_id=request_id)
            self._send_turn(conn, agent, prompt, "continue_prompt", request_id=request_id)
            return
        if phase == "evaluate_terminal":
            request_id = _new_request_id()
            prompt = build_terminal_prompt(flow, state, agent, allow_keep_working=bool(state.prompt), request_id=request_id)
            self._send_turn(conn, agent, prompt, "terminal_eval", request_id=request_id)
            return
        if phase == "evaluate_transition":
            request_id = _new_request_id()
            prompt = build_transition_prompt(flow, state, agent, allow_keep_working=bool(state.prompt), request_id=request_id)
            self._send_turn(conn, agent, prompt, "transition_eval", request_id=request_id)

    def _send_turn(self, conn: Any, agent: dict[str, Any], prompt: str, kind: str, *, request_id: str) -> None:
        submitted_at = format_utc(utc_now())
        pending_fields: dict[str, str] = {
            "current_turn_kind": kind,
            "current_turn_started_at": submitted_at,
            "current_turn_id": "",
            "current_request_id": request_id,
            "phase": "submitting",
            "status_message": f"Submitting {kind}",
        }
        update_agent(conn, int(agent["id"]), **pending_fields)
        agent.update(pending_fields)

        observation = self.backend.send_prompt(agent, prompt, request_id=request_id)
        now = observation.started_at or submitted_at
        fields: dict[str, str] = {
            "current_turn_kind": kind,
            "current_turn_started_at": now,
            "current_turn_id": observation.turn_id or "",
            "current_request_id": request_id,
            "last_prompt_sent_at": now,
            "phase": "working",
            "status_message": f"Waiting for {kind}",
        }
        if observation.thread_id:
            fields["thread_id"] = observation.thread_id
        if observation.rollout_path:
            fields["rollout_path"] = observation.rollout_path
        update_agent(conn, int(agent["id"]), **fields)

    def _handle_completed_turn(
        self,
        conn: Any,
        agent: dict[str, Any],
        flow: FlowSpec,
        state: StateSpec,
        output_text: str,
    ) -> None:
        kind = agent["current_turn_kind"]
        request_id = str(agent.get("current_request_id") or "")
        update_agent(
            conn,
            int(agent["id"]),
            current_turn_id="",
            current_turn_kind="",
            current_turn_started_at="",
            current_request_id="",
        )
        if kind == "terminal_eval":
            decision = parse_decision(output_text, expected_request_id=request_id)
            if decision.choice == IMPLICIT_TRANSITION_NEEDS_HELP:
                close_open_state_run(conn, int(agent["id"]))
                record_agent_event(
                    conn,
                    int(agent["id"]),
                    "decision",
                    from_state=state.name,
                    choice=IMPLICIT_TRANSITION_NEEDS_HELP,
                    reason=decision.reason,
                )
                update_agent(
                    conn,
                    int(agent["id"]),
                    substate="needs_help",
                    phase="paused",
                    status_message="Needs help",
                    last_error=decision.reason,
                )
                return
            if decision.choice == IMPLICIT_TRANSITION_KEEP_WORKING:
                record_agent_event(
                    conn,
                    int(agent["id"]),
                    "decision",
                    from_state=state.name,
                    choice=IMPLICIT_TRANSITION_KEEP_WORKING,
                    reason=decision.reason,
                )
                update_agent(conn, int(agent["id"]), phase="continue_state", status_message=decision.reason)
                return
            if decision.choice == IMPLICIT_TRANSITION_WAIT_FOR_CHILD:
                self._park_for_child_wait(conn, agent, state, decision)
                return
            if decision.choice != IMPLICIT_TRANSITION_FINISH:
                close_open_state_run(conn, int(agent["id"]))
                record_agent_event(
                    conn,
                    int(agent["id"]),
                    "decision",
                    from_state=state.name,
                    choice=IMPLICIT_TRANSITION_NEEDS_HELP,
                    reason=f"Invalid transition choice '{decision.choice}'",
                )
                update_agent(
                    conn,
                    int(agent["id"]),
                    substate="needs_help",
                    phase="paused",
                    last_error=f"Invalid transition choice '{decision.choice}'",
                    status_message="Needs help",
                )
                return
            record_agent_event(
                conn,
                int(agent["id"]),
                "decision",
                from_state=state.name,
                to_state=state.name,
                choice=IMPLICIT_TRANSITION_FINISH,
                reason=decision.reason,
            )
            self._transition_terminal(
                conn,
                agent,
                state.name,
                choice=IMPLICIT_TRANSITION_FINISH,
                reason=decision.reason or "Finished terminal end state",
            )
            return
        if kind == "transition_eval":
            decision = parse_decision(output_text, expected_request_id=request_id)
            if decision.choice == IMPLICIT_TRANSITION_NEEDS_HELP:
                close_open_state_run(conn, int(agent["id"]))
                record_agent_event(
                    conn,
                    int(agent["id"]),
                    "decision",
                    from_state=state.name,
                    choice=IMPLICIT_TRANSITION_NEEDS_HELP,
                    reason=decision.reason,
                )
                update_agent(
                    conn,
                    int(agent["id"]),
                    substate="needs_help",
                    phase="paused",
                    status_message="Needs help",
                    last_error=decision.reason,
                )
                return
            if decision.choice == IMPLICIT_TRANSITION_KEEP_WORKING:
                record_agent_event(
                    conn,
                    int(agent["id"]),
                    "decision",
                    from_state=state.name,
                    choice=IMPLICIT_TRANSITION_KEEP_WORKING,
                    reason=decision.reason,
                )
                update_agent(conn, int(agent["id"]), phase="continue_state", status_message=decision.reason)
                return
            if decision.choice == IMPLICIT_TRANSITION_WAIT_FOR_CHILD:
                self._park_for_child_wait(conn, agent, state, decision)
                return
            transition = _selected_transition(state, decision.choice)
            if transition is None:
                close_open_state_run(conn, int(agent["id"]))
                record_agent_event(
                    conn,
                    int(agent["id"]),
                    "decision",
                    from_state=state.name,
                    choice=IMPLICIT_TRANSITION_NEEDS_HELP,
                    reason=f"Invalid transition choice '{decision.choice}'",
                )
                update_agent(
                    conn,
                    int(agent["id"]),
                    substate="needs_help",
                    phase="paused",
                    last_error=f"Invalid transition choice '{decision.choice}'",
                    status_message="Needs help",
                )
                return
            record_transition(conn, int(agent["id"]), state.name, decision.choice, decision.choice, decision.reason, decision.raw_json)
            record_agent_event(
                conn,
                int(agent["id"]),
                "decision",
                from_state=state.name,
                to_state=decision.choice,
                choice=decision.choice,
                reason=decision.reason,
            )
            self._move_to_state(conn, agent, flow, decision.choice, decision.reason, transition=transition)
            return
        if kind == "children_wake":
            update_agent(conn, int(agent["id"]), pending_state_json="")
        next_phase = "evaluate_terminal" if state.end else "evaluate_transition"
        next_status = "Evaluating terminal state" if state.end else "Evaluating transitions"
        update_agent(conn, int(agent["id"]), phase=next_phase, status_message=next_status)

    def _park_for_child_wait(self, conn: Any, agent: dict[str, Any], state: StateSpec, decision: Decision) -> None:
        agent_id = int(agent["id"])
        child_wait = _child_wait_snapshot(conn, decision.child_ids)
        pending = [item["id"] for item in child_wait["pending"]]
        if pending:
            now = format_utc(utc_now())
            close_open_state_run(conn, agent_id, ended_at=now)
            record_agent_event(
                conn,
                agent_id,
                "wait_children",
                state_name=state.name,
                reason=decision.reason,
                payload={"child_ids": list(decision.child_ids), "pending": pending, "started_at": now},
            )
            update_agent(
                conn,
                agent_id,
                phase="waiting_children",
                status_message=_waiting_children_status(pending),
                pending_state_json=json.dumps(
                    {"kind": "waiting_children", "child_ids": list(decision.child_ids), "started_at": now},
                    sort_keys=True,
                ),
            )
            return
        self._queue_children_wake(
            conn,
            agent,
            {"started_at": format_utc(utc_now()), "pending": [], "finished": child_wait["finished"]},
        )

    def _move_to_state(
        self,
        conn: Any,
        agent: dict[str, Any],
        flow: FlowSpec,
        state_name: str,
        reason: str,
        *,
        transition: TransitionSpec | None = None,
        pause: bool = False,
    ) -> None:
        now_dt = utc_now()
        now = format_utc(now_dt)
        close_open_state_run(conn, int(agent["id"]), ended_at=now)
        next_state = flow.states[state_name]
        wait_text = transition.wait if transition is not None and transition.wait is not None else next_state.wait
        if next_state.end and not next_state.prompt and not wait_text and not pause:
            update_agent(
                conn,
                int(agent["id"]),
                current_state=state_name,
                state_entered_at=now,
                ready_at="",
                ended_at=now,
                phase="finished",
                substate="normal",
                current_turn_id="",
                current_turn_kind="",
                current_turn_started_at="",
                current_request_id="",
                status_message=reason,
            )
            self.backend.terminate(agent, immediate=False)
            return
        ready_at = ""
        phase = "paused" if pause else "enter_state"
        if wait_text and not pause:
            ready_at = format_utc(now_dt + timedelta(seconds=parse_wait_seconds(wait_text)))
            phase = "waiting"
        updates = {
            "current_state": state_name,
            "state_entered_at": now,
            "ready_at": ready_at,
            "phase": phase,
            "status_message": f"{reason}; paused until resume" if pause else (reason if not ready_at else f"Waiting until {ready_at}"),
            "pending_state_json": "",
            "current_turn_id": "",
            "current_turn_kind": "",
            "current_turn_started_at": "",
            "current_request_id": "",
        }
        if pause:
            updates["substate"] = "interaction"
        update_agent(conn, int(agent["id"]), **updates)
        if ready_at:
            record_agent_event(
                conn,
                int(agent["id"]),
                "delay",
                state_name=state_name,
                reason=f"Waiting for {wait_text}",
                payload={"wait": wait_text, "ready_at": ready_at},
            )
        if not ready_at and not pause:
            open_state_run(conn, int(agent["id"]), state_name, started_at=now)

    def _transition_terminal(self, conn: Any, agent: dict[str, Any], state_name: str, *, choice: str, reason: str) -> None:
        now = format_utc(utc_now())
        close_open_state_run(conn, int(agent["id"]), ended_at=now)
        update_agent(
            conn,
            int(agent["id"]),
            current_state=state_name,
            ended_at=now,
            phase="finished",
            substate="normal",
            current_turn_id="",
            current_turn_kind="",
            current_turn_started_at="",
            current_request_id="",
            ready_at="",
            status_message=reason,
        )
        self.backend.terminate(agent, immediate=False)

    def _agent_flow(self, conn: Any, agent: dict[str, Any]) -> FlowSpec:
        snapshot = get_flow_snapshot(conn, int(agent["flow_snapshot_id"]))
        payload = json.loads(snapshot["snapshot_json"])
        return flow_from_dict(payload)

    def _handle_waiting_children(self, conn: Any, agent: dict[str, Any], state: StateSpec) -> bool:
        if agent["phase"] != "waiting_children":
            return False
        child_wait = _child_wait_state(conn, agent)
        if child_wait is None:
            update_agent(conn, int(agent["id"]), phase="resume_state", status_message="Child wait metadata was lost")
            open_state_run(conn, int(agent["id"]), state.name)
            return False
        pending = [item["id"] for item in child_wait["pending"]]
        if pending:
            status = _waiting_children_status(pending)
            if status != str(agent.get("status_message") or ""):
                update_agent(conn, int(agent["id"]), status_message=status)
            return True
        self._queue_children_wake(conn, agent, child_wait)
        return True

    def _queue_children_wake(self, conn: Any, agent: dict[str, Any], child_wait: dict[str, Any]) -> None:
        agent_id = int(agent["id"])
        state_name = str(agent["current_state"])
        finished = child_wait["finished"]
        record_agent_event(
            conn,
            agent_id,
            "wake_children",
            state_name=state_name,
            reason=_children_wake_reason(finished),
            payload={"children": finished, "started_at": child_wait["started_at"]},
        )
        update_agent(
            conn,
            agent_id,
            phase="resume_state",
            status_message="Children finished",
            pending_state_json=json.dumps(
                {
                    "kind": "children_wake",
                    "children": finished,
                    "started_at": child_wait["started_at"],
                    "add_dirs": [item["scratchpad_dir"] for item in finished if item.get("scratchpad_dir")],
                },
                sort_keys=True,
            ),
        )
        if latest_open_state_run(conn, agent_id) is None:
            open_state_run(conn, agent_id, state_name)

    def _handle_delayed_entry(self, conn: Any, agent: dict[str, Any], state: StateSpec) -> bool:
        remaining = _waiting_remaining_seconds(agent)
        if remaining > 0:
            if agent["phase"] != "waiting":
                update_agent(conn, int(agent["id"]), phase="waiting")
            return True
        if not agent.get("ready_at"):
            if agent["phase"] == "enter_state" and state.wait:
                close_open_state_run(conn, int(agent["id"]))
                ready_at = format_utc(utc_now() + timedelta(seconds=parse_wait_seconds(state.wait)))
                record_agent_event(
                    conn,
                    int(agent["id"]),
                    "delay",
                    state_name=state.name,
                    reason=f"Waiting for {state.wait}",
                    payload={"wait": state.wait, "ready_at": ready_at},
                )
                update_agent(
                    conn,
                    int(agent["id"]),
                    ready_at=ready_at,
                    phase="waiting",
                    status_message=f"Waiting until {ready_at}",
                )
                return True
            return False

        update_agent(
            conn,
            int(agent["id"]),
            ready_at="",
            phase="enter_state",
            status_message="Delay elapsed",
        )
        open_state_run(conn, int(agent["id"]), state.name)
        agent["ready_at"] = ""
        agent["phase"] = "enter_state"
        return False

    def _tick_paused_agent(self, conn: Any, agent: dict[str, Any]) -> None:
        if not agent["current_turn_started_at"]:
            return
        observation = self.backend.poll_turn(agent)
        self._apply_turn_observation_metadata(conn, agent, observation)
        if observation.status == "aborted":
            status_message = "Needs help" if agent["substate"] == "needs_help" else "Paused in interaction"
            update_agent(
                conn,
                int(agent["id"]),
                current_turn_id="",
                current_turn_kind="",
                current_turn_started_at="",
                current_request_id="",
                phase="paused",
                status_message=status_message,
            )
            return
        if observation.status != "completed":
            return
        status_message = "Needs help" if agent["substate"] == "needs_help" else "Paused in interaction"
        update_agent(
            conn,
            int(agent["id"]),
            current_turn_id="",
            current_turn_kind="",
            current_turn_started_at="",
            current_request_id="",
            phase="paused",
            status_message=status_message,
        )

    def _apply_turn_observation_metadata(self, conn: Any, agent: dict[str, Any], observation: Any) -> None:
        updates: dict[str, str] = {}
        if observation.thread_id and not agent["thread_id"]:
            updates["thread_id"] = observation.thread_id
            agent["thread_id"] = observation.thread_id
        if observation.rollout_path and observation.rollout_path != agent["rollout_path"]:
            updates["rollout_path"] = observation.rollout_path
            agent["rollout_path"] = observation.rollout_path
        if updates:
            update_agent(conn, int(agent["id"]), **updates)

    def _maybe_set_thread_name(self, conn: Any, agent: dict[str, Any], flow: FlowSpec) -> None:
        agent_id = int(agent["id"])
        if _thread_name_result_recorded(conn, agent_id):
            return
        name = _suggested_thread_name(flow, agent).strip()
        if not name:
            return
        result = self.backend.set_thread_name(agent, name)
        if result is None:
            return
        kind = "thread_name_set" if result else "thread_name_set_failed"
        reason = f"Set thread name to {name}" if result else f"Failed to set thread name to {name}"
        record_agent_event(conn, agent_id, kind, state_name=agent["current_state"], reason=reason, payload={"name": name})

    def _enter_needs_help(self, conn: Any, agent: dict[str, Any], reason: str) -> None:
        close_open_state_run(conn, int(agent["id"]))
        if reason != str(agent.get("last_error") or ""):
            record_agent_event(
                conn,
                int(agent["id"]),
                "needs_help",
                state_name=agent["current_state"],
                reason=reason,
            )
        updates = {
            "substate": "needs_help",
            "phase": "paused",
            "last_error": reason,
            "status_message": "Needs help",
        }
        if not (agent.get("phase") == "submitting" and agent.get("current_turn_started_at")):
            updates.update(
                {
                    "current_turn_id": "",
                    "current_turn_kind": "",
                    "current_turn_started_at": "",
                    "current_request_id": "",
                }
            )
        update_agent(conn, int(agent["id"]), **updates)


def build_state_prompt(flow: FlowSpec, state: StateSpec, agent: dict[str, Any], *, request_id: str) -> str:
    setup = _initial_setup_guidance(agent)
    scratchpad = "" if setup else _scratchpad_file_line(agent)
    lines = [
        f"Flow: {flow.name}",
        f"State: {state.name}",
        "",
    ]
    if setup:
        lines.extend([setup, ""])
    else:
        lines.extend([scratchpad, ""])
    lines.extend(
        [
            "Work on the following state instructions:",
            state.prompt.strip(),
        ]
    )
    return _control_wrapped_prompt(
        flow,
        agent,
        "state_prompt",
        "\n".join(lines).strip(),
        request_id=request_id,
    )


def build_continue_prompt(flow: FlowSpec, state: StateSpec, agent: dict[str, Any], *, request_id: str) -> str:
    setup = _initial_setup_guidance(agent)
    scratchpad = "" if setup else _scratchpad_file_line(agent)
    lines = [f"Continue working in state '{state.name}'."]
    if setup:
        lines.extend(["", setup])
    else:
        lines.extend(["", scratchpad])
    lines.extend(
        [
            "",
            "Use your best judgement and keep pushing the current state forward.",
            "Do not evaluate transitions yet; keep working until the runtime asks again.",
        ]
    )
    return _control_wrapped_prompt(
        flow,
        agent,
        "continue_prompt",
        "\n".join(lines),
        request_id=request_id,
    )


def build_transition_prompt(
    flow: FlowSpec,
    state: StateSpec,
    agent: dict[str, Any],
    *,
    allow_keep_working: bool,
    request_id: str,
) -> str:
    lines = [
        f"You are evaluating outgoing transitions for flow '{flow.name}' state '{state.name}'.",
        "Choose exactly one transition name.",
        "",
    ]
    setup = _initial_setup_guidance(agent)
    scratchpad = "" if setup else _scratchpad_file_line(agent)
    if setup:
        lines.extend([setup, ""])
    else:
        lines.extend([scratchpad, ""])
    lines.append("Explicit transitions:")
    for transition in state.transitions:
        condition = transition.condition or "(unconditional)"
        wait_suffix = f" [wait {transition.wait}]" if transition.wait else ""
        lines.append(f"- {transition.target}: {condition}{wait_suffix}")
    lines.extend(
        [
            "",
            "Implicit transitions:",
            f"- {IMPLICIT_TRANSITION_NEEDS_HELP}: choose this if you are blocked or need human input.",
            (
                f"- {IMPLICIT_TRANSITION_WAIT_FOR_CHILD}: choose this if you started one or more child agents and"
                " want the runtime to wake you after they all finish. Include a 'child_ids' list alongside 'choice'."
            ),
        ]
    )
    if allow_keep_working:
        lines.append(
            f"- {IMPLICIT_TRANSITION_KEEP_WORKING}: choose this if more work in the current state is the best action."
        )
    lines.extend(
        [
            "",
            (
                'Respond with JSON only in the form '
                f'{{"request_id": "{request_id}", "choice": "<name>", "reason": "<short explanation>"}}.'
            ),
            (
                f'When choosing {IMPLICIT_TRANSITION_WAIT_FOR_CHILD}, also include "child_ids": [17, 18].'
            ),
        ]
    )
    return _control_wrapped_prompt(flow, agent, "transition_eval", "\n".join(lines), request_id=request_id)


def build_terminal_prompt(
    flow: FlowSpec,
    state: StateSpec,
    agent: dict[str, Any],
    *,
    allow_keep_working: bool,
    request_id: str,
) -> str:
    lines = [
        f"You are evaluating terminal completion for flow '{flow.name}' state '{state.name}'.",
        "Choose exactly one terminal action name.",
        "",
    ]
    setup = _initial_setup_guidance(agent)
    scratchpad = "" if setup else _scratchpad_file_line(agent)
    if setup:
        lines.extend([setup, ""])
    else:
        lines.extend([scratchpad, ""])
    lines.extend(
        [
            "Terminal actions:",
            f"- {IMPLICIT_TRANSITION_FINISH}: choose this if the work for this terminal state is complete and the agent should finish.",
            f"- {IMPLICIT_TRANSITION_NEEDS_HELP}: choose this if you are blocked or need human input.",
            (
                f"- {IMPLICIT_TRANSITION_WAIT_FOR_CHILD}: choose this if you started one or more child agents and"
                " want the runtime to wake you after they all finish. Include a 'child_ids' list alongside 'choice'."
            ),
        ]
    )
    if allow_keep_working:
        lines.append(
            f"- {IMPLICIT_TRANSITION_KEEP_WORKING}: choose this if more work in the current terminal state is the best action."
        )
    lines.extend(
        [
            "",
            (
                'Respond with JSON only in the form '
                f'{{"request_id": "{request_id}", "choice": "<name>", "reason": "<short explanation>"}}.'
            ),
            (
                f'When choosing {IMPLICIT_TRANSITION_WAIT_FOR_CHILD}, also include "child_ids": [17, 18].'
            ),
        ]
    )
    return _control_wrapped_prompt(flow, agent, "terminal_eval", "\n".join(lines), request_id=request_id)


def _children_wake_prompt(flow: FlowSpec, state: StateSpec, agent: dict[str, Any], *, request_id: str = "") -> str:
    payload = pending_state_payload(agent)
    if payload.get("kind") != "children_wake":
        return ""
    lines = [
        f"Flow: {flow.name}",
        f"State: {state.name}",
        "",
        "Child flows have finished.",
        "",
    ]
    children = payload.get("children") or []
    if isinstance(children, list) and children:
        lines.append("Children finished:")
        for item in children:
            if not isinstance(item, dict):
                continue
            child_id = str(item.get("id") or "?")
            flow_name = str(item.get("flow") or "").strip()
            label = f"agent {child_id}"
            if flow_name:
                label += f" ({flow_name})"
            lines.append(f"- {label} -> {_child_result_label(item)}")
        lines.append("")
    scratchpads = [str(item.get("scratchpad_path") or "").strip() for item in children if isinstance(item, dict)]
    scratchpads = [item for item in scratchpads if item]
    if scratchpads:
        lines.append("Scratchpads available for this turn:")
        for path in scratchpads:
            lines.append(f"- {path}")
        lines.extend(["", "Treat child scratchpads as read-only context for this turn.", ""])
    lines.extend(
        [
            _scratchpad_file_line(agent),
            "",
            "Inspect the child results, read any relevant scratchpads, update your own scratchpad if needed, and continue the current state.",
            "Do not choose a transition yet; the runtime will ask right after this turn.",
        ]
    )
    return _control_wrapped_prompt(flow, agent, "children_wake", "\n".join(lines), request_id=request_id)


def parse_decision(text: str, *, expected_request_id: str = "") -> Decision:
    raw = text.strip()
    if raw.startswith("```"):
        parts = raw.splitlines()
        if len(parts) >= 3 and parts[-1].strip() == "```":
            raw = "\n".join(parts[1:-1]).strip()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("transition evaluation did not return a JSON object")
    reason = payload.get("reason") or ""
    if not isinstance(reason, str):
        raise ValueError("transition evaluation JSON has non-string 'reason'")

    choice = payload.get("choice")
    if not isinstance(choice, str) or not choice.strip():
        raise ValueError("transition evaluation JSON is missing 'choice'")
    request_id = payload.get("request_id") or ""
    if expected_request_id:
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("transition evaluation JSON is missing 'request_id'")
        if request_id.strip() != expected_request_id:
            raise ValueError(
                f"transition evaluation JSON request_id mismatch: expected '{expected_request_id}', got '{request_id.strip()}'"
            )
    normalized = normalize_implicit_transition_name(choice)
    child_ids: tuple[int, ...] = ()
    if normalized == IMPLICIT_TRANSITION_WAIT_FOR_CHILD:
        child_ids = _parse_child_ids(payload.get("child_ids"))
    return Decision(
        choice=normalized,
        reason=reason.strip(),
        raw_json=raw,
        request_id=request_id.strip() if isinstance(request_id, str) else "",
        child_ids=child_ids,
    )


def _parse_child_ids(value: Any) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{IMPLICIT_TRANSITION_WAIT_FOR_CHILD} requires a non-empty 'child_ids' list")
    ordered: list[int] = []
    seen: set[int] = set()
    for item in value:
        try:
            child_id = int(item)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{IMPLICIT_TRANSITION_WAIT_FOR_CHILD} child_ids must contain integers"
            ) from exc
        if child_id <= 0:
            raise ValueError(
                f"{IMPLICIT_TRANSITION_WAIT_FOR_CHILD} child_ids must contain positive integers"
            )
        if child_id in seen:
            continue
        seen.add(child_id)
        ordered.append(child_id)
    if not ordered:
        raise ValueError(f"{IMPLICIT_TRANSITION_WAIT_FOR_CHILD} requires at least one child id")
    return tuple(ordered)


def _thread_name_result_recorded(conn: Any, agent_id: int) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM agent_events
        WHERE agent_id=? AND kind IN ('thread_name_set', 'thread_name_set_failed')
        LIMIT 1
        """,
        (agent_id,),
    ).fetchone()
    return row is not None


def _new_request_id() -> str:
    return f"req-{uuid.uuid4().hex[:12]}"


def _control_wrapped_prompt(flow: FlowSpec, agent: dict[str, Any], kind: str, body: str, *, request_id: str = "") -> str:
    marker = agent.get("launch_marker") or f"flow-{agent['id']}-{uuid.uuid4().hex[:8]}"
    lines: list[str] = []
    thread_name_hint = _thread_name_hint(flow, agent)
    if thread_name_hint:
        lines.extend([thread_name_hint, ""])
    lines.extend(
        [
            "[flow-control]",
            f"agent_id: {agent['id']}",
            f"marker: {marker}",
            f"kind: {kind}",
            *( [f"request_id: {request_id}"] if request_id else [] ),
            "[/flow-control]",
            "",
            body.strip(),
        ]
    )
    return "\n".join(lines)


def _thread_name_hint(flow: FlowSpec, agent: dict[str, Any]) -> str:
    if agent.get("last_prompt_sent_at"):
        return ""
    return _suggested_thread_name(flow, agent)


def _suggested_thread_name(flow: FlowSpec, agent: dict[str, Any]) -> str:
    parts = [f"[flow {agent['id']}]", flow.name]
    parts.extend(_non_default_arg_tokens(flow, agent))
    return " ".join(part for part in parts if part)


def _non_default_arg_tokens(flow: FlowSpec, agent: dict[str, Any]) -> list[str]:
    raw_args = agent.get("args_json") or "{}"
    try:
        values = json.loads(raw_args)
    except json.JSONDecodeError:
        return []
    if not isinstance(values, dict):
        return []

    tokens: list[str] = []
    for name in sorted(values):
        value = values.get(name)
        if value is None:
            continue
        rendered = str(value)
        spec = flow.args.get(name)
        default = None if spec is None or spec.default is None else str(spec.default)
        if default is not None and rendered == default:
            continue
        tokens.append(f"{name}={rendered}")
    return tokens


def _initial_setup_guidance(agent: dict[str, Any]) -> str:
    if agent.get("last_prompt_sent_at"):
        return ""
    path = scratchpad_path_text(agent)
    return "\n".join(
        [
            "Flow runtime:",
            "- You are running under the flow harness and will be moved through named states by the runtime.",
            "- Some later prompts will ask you to choose transitions or terminal actions in a strict format; when they do, follow that format exactly.",
            "",
            f"Scratchpad file: {path}",
            "Purpose: lightweight durable memory across future turns and states.",
            "Use it only when there is genuinely reusable state worth preserving.",
            "Avoid: logs, workbooks, and required checklists.",
            "Optional headings:",
            "- Context: short background needed to orient the next state",
            "- Settled: established facts or conclusions that should not need to be rediscovered",
            "- Watch: live or time-sensitive items that may need checking again later",
            "- Notes: optional extra state worth carrying forward",
        ]
    )


def _pending_kind(agent: dict[str, Any]) -> str:
    return str(pending_state_payload(agent).get("kind") or "")


def _child_wait_state(conn: Any, agent: dict[str, Any]) -> dict[str, Any] | None:
    payload = pending_state_payload(agent)
    if payload.get("kind") != "waiting_children":
        return None
    child_ids = payload.get("child_ids") or []
    if not isinstance(child_ids, list):
        return None
    started_at = str(payload.get("started_at") or "")
    return {"started_at": started_at, **_child_wait_snapshot(conn, child_ids)}


def _child_wait_snapshot(conn: Any, child_ids: list[int] | tuple[int, ...]) -> dict[str, list[dict[str, Any]]]:
    pending: list[dict[str, Any]] = []
    finished: list[dict[str, Any]] = []
    for child_id in child_ids:
        row = get_agent(conn, int(child_id))
        if row is None:
            finished.append({"id": int(child_id), "flow": "", "end_state": None, "status": "unknown"})
            continue
        agent = dict(row)
        if not agent.get("ended_at"):
            pending.append({"id": int(child_id), "flow": str(agent.get("flow_name") or "")})
            continue
        scratchpad_dir = str(agent_scratchpad_dir(int(child_id)))
        finished.append(
            {
                "id": int(child_id),
                "flow": str(agent.get("flow_name") or ""),
                "end_state": str(agent.get("current_state") or "") or None,
                "status": "stopped" if str(agent.get("current_state") or "") == "stopped" else "finished",
                "scratchpad_path": scratchpad_path_text(agent),
                "scratchpad_dir": scratchpad_dir,
            }
        )
    return {"pending": pending, "finished": finished}


def _waiting_children_status(child_ids: list[int]) -> str:
    labels = ", ".join(f"#{child_id}" for child_id in child_ids)
    return f"Waiting on child agents {labels}"


def _children_wake_reason(children: list[dict[str, Any]]) -> str:
    if not children:
        return "Children finished"
    labels = ", ".join(f"#{int(item['id'])}" for item in children if "id" in item)
    return f"Children finished: {labels}" if labels else "Children finished"


def _child_result_label(item: dict[str, Any]) -> str:
    status = str(item.get("status") or "")
    end_state = str(item.get("end_state") or "")
    if status == "unknown":
        return "(unknown)"
    if status == "stopped" or end_state == "stopped":
        return "(stopped)"
    return end_state or "(finished)"


def _scratchpad_file_line(agent: dict[str, Any]) -> str:
    return f"Scratchpad file: {scratchpad_path_text(agent)}"


def _selected_transition(state: StateSpec, choice: str) -> TransitionSpec | None:
    for transition in state.transitions:
        if transition.target == choice:
            return transition
    return None


def _state_fast_value(state_fast: bool | None, flow_fast: bool | None, agent_fast: Any) -> bool:
    if state_fast is not None:
        return state_fast
    if flow_fast is not None:
        return flow_fast
    return bool(agent_fast)


def _auto_transition(state: StateSpec) -> TransitionSpec | None:
    if state.prompt:
        return None
    if len(state.transitions) != 1:
        return None
    transition = state.transitions[0]
    if transition.condition:
        return None
    return transition


def _waiting_remaining_seconds(agent: dict[str, Any]) -> float:
    ready_at = parse_utc(agent.get("ready_at"))
    if ready_at is None:
        return 0.0
    return max(0.0, (ready_at - utc_now()).total_seconds())
