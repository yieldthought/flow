"""Background runtime loop."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from .backend import AgentBackend, CodexBackend
from .common import (
    IMPLICIT_TRANSITION_KEEP_WORKING,
    IMPLICIT_TRANSITION_NEEDS_HELP,
    RESERVED_STATE_NAMES,
    current_actor,
    format_utc,
    normalize_phase,
    parse_wait_seconds,
    parse_utc,
    utc_now,
)
from .flowfile import FlowSpec, StateSpec, TransitionSpec, flow_from_dict
from .store import (
    clear_daemon_status,
    close_open_state_run,
    connect,
    get_agent,
    get_flow_snapshot,
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
                self.tick(conn)
                time.sleep(POLL_INTERVAL_SECONDS)
        except Exception as exc:  # pragma: no cover - defensive daemon guard
            exit_code = 1
            exited_at = format_utc(utc_now())
            details = traceback.format_exc().rstrip()
            record_daemon_event(
                conn,
                level="error",
                message=str(exc),
                created_at=exited_at,
                details_text=details,
            )
            record_daemon_exit(conn, kind="error", exited_at=exited_at, error_text=details)
            conn.commit()
            print(details, file=sys.stderr)
        finally:
            if exit_code == 0:
                record_daemon_exit(conn, kind="clean", exited_at=exited_at or format_utc(utc_now()))
            clear_daemon_status(conn)
            conn.commit()
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
                    if str(exc) != str(agent.get("last_error") or ""):
                        record_agent_event(
                            conn,
                            int(agent["id"]),
                            "error",
                            state_name=agent["current_state"],
                            reason=str(exc),
                        )
                        record_daemon_event(
                            conn,
                            level="warning",
                            message=f"agent #{agent['id']} {agent['flow_name']}:{agent['current_state']} {exc}",
                            details_text=details,
                        )
                    update_agent(
                        conn,
                        int(agent["id"]),
                        last_error=str(exc),
                        status_message=str(exc),
                    )

    def _recover_agents_after_restart(self, conn: Any) -> None:
        if self._recovered:
            return
        for row in list_agents(conn):
            agent = dict(row)
            if agent["ended_at"]:
                continue
            phase = normalize_phase(agent["phase"])
            fields = {
                "current_turn_id": "",
                "current_turn_kind": "",
                "current_turn_started_at": "",
                "last_error": "",
            }
            if agent["substate"] == "normal":
                if phase == "waiting":
                    fields["phase"] = "waiting"
                elif phase == "enter_state" and not agent["thread_id"] and not agent["last_prompt_sent_at"]:
                    fields["phase"] = "enter_state"
                else:
                    fields["phase"] = "resume_state"
            else:
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
            error = ""
            try:
                self._apply_command(conn, agent, row["kind"], payload)
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
                        payload={"command": row["kind"]},
                    )
                    record_daemon_event(
                        conn,
                        level="warning",
                        message=f"command {row['kind']} failed for agent #{agent['id']}: {error}",
                        details_text=details,
                    )
                update_agent(conn, int(agent["id"]), last_error=error, status_message=error)
            mark_command_processed(conn, int(row["id"]), error)

    def _apply_command(self, conn: Any, agent: dict[str, Any], kind: str, payload: dict[str, Any]) -> None:
        agent_id = int(agent["id"])
        actor = current_actor()
        if kind == "pause":
            close_open_state_run(conn, agent_id)
            reason = f"Paused by {actor}"
            record_agent_event(
                conn,
                agent_id,
                "pause",
                state_name=agent["current_state"],
                reason=reason,
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
            self.backend.interrupt(agent)
            close_open_state_run(conn, agent_id)
            record_agent_event(
                conn,
                agent_id,
                "interrupt",
                state_name=agent["current_state"],
                reason=f"Interrupted by {actor}",
            )
            update_agent(conn, agent_id, substate="interaction", phase="paused", current_turn_id="", current_turn_kind="", current_turn_started_at="")
            return
        if kind == "resume":
            fields = {"substate": "normal", "shutdown_mode": ""}
            remaining = _waiting_remaining_seconds(agent)
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
            )
            update_agent(conn, agent_id, **fields)
            return
        if kind == "wake":
            if not agent.get("ready_at"):
                raise ValueError(f"agent {agent_id} is not currently waiting")
            reason = f"Woken by {actor}"
            record_agent_event(conn, agent_id, "wake", state_name=agent["current_state"], reason=reason)
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
            update_agent(conn, agent_id, substate="normal", current_turn_id="", current_turn_kind="", current_turn_started_at="")
            reason = f"Moved to {target} by {actor}"
            record_agent_event(
                conn,
                agent_id,
                "decision",
                from_state=agent["current_state"],
                to_state=target,
                choice="move",
                reason=reason,
            )
            self._move_to_state(conn, agent, flow, target, reason)
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
            )
            self._transition_terminal(conn, agent, target, choice="stop", reason=f"Stopped by {actor}")
            return
        if kind == "delete":
            self.backend.terminate(agent, immediate=True)
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

        if state.end:
            self._transition_terminal(conn, agent, state.name, choice="state_end", reason="Reached end state")
            return

        if self._handle_delayed_entry(conn, agent, state):
            return

        agent["desired_mode"] = state.mode or flow.mode or agent["mode"]
        agent["desired_thinking"] = state.thinking or flow.thinking or agent["thinking"]

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

        if agent["current_turn_started_at"]:
            observation = self.backend.poll_turn(agent)
            self._apply_turn_observation_metadata(conn, agent, observation)
            if observation.status != "completed":
                return
            update_agent(conn, int(agent["id"]), current_turn_id=observation.turn_id, status_message="Turn completed")
            agent["current_turn_id"] = observation.turn_id
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

        phase = agent["phase"]
        if agent["shutdown_mode"]:
            self.backend.terminate(agent, immediate=False)
            update_agent(conn, int(agent["id"]), shutdown_mode="", phase="resume_state")
            return
        if phase in {"enter_state", "resume_state"}:
            if state.prompt:
                prompt = build_state_prompt(flow, state, agent)
                self._send_turn(conn, agent, prompt, "state_prompt" if phase == "enter_state" else "resume_prompt")
            else:
                prompt = build_transition_prompt(flow, state, agent, allow_keep_working=False)
                self._send_turn(conn, agent, prompt, "transition_eval")
            return
        if phase == "continue_state":
            prompt = build_continue_prompt(flow, state, agent)
            self._send_turn(conn, agent, prompt, "continue_prompt")
            return
        if phase == "evaluate_transition":
            prompt = build_transition_prompt(flow, state, agent, allow_keep_working=bool(state.prompt))
            self._send_turn(conn, agent, prompt, "transition_eval")

    def _send_turn(self, conn: Any, agent: dict[str, Any], prompt: str, kind: str) -> None:
        self.backend.send_prompt(agent, prompt)
        now = format_utc(utc_now())
        update_agent(
            conn,
            int(agent["id"]),
            current_turn_kind=kind,
            current_turn_started_at=now,
            current_turn_id="",
            last_prompt_sent_at=now,
            phase="working",
            status_message=f"Waiting for {kind}",
        )

    def _handle_completed_turn(
        self,
        conn: Any,
        agent: dict[str, Any],
        flow: FlowSpec,
        state: StateSpec,
        output_text: str,
    ) -> None:
        kind = agent["current_turn_kind"]
        update_agent(
            conn,
            int(agent["id"]),
            current_turn_id="",
            current_turn_kind="",
            current_turn_started_at="",
        )
        if kind == "transition_eval":
            decision = parse_decision(output_text)
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
        update_agent(conn, int(agent["id"]), phase="evaluate_transition", status_message="Evaluating transitions")

    def _move_to_state(
        self,
        conn: Any,
        agent: dict[str, Any],
        flow: FlowSpec,
        state_name: str,
        reason: str,
        *,
        transition: TransitionSpec | None = None,
    ) -> None:
        now_dt = utc_now()
        now = format_utc(now_dt)
        close_open_state_run(conn, int(agent["id"]), ended_at=now)
        next_state = flow.states[state_name]
        wait_text = transition.wait if transition is not None and transition.wait is not None else next_state.wait
        if next_state.end:
            update_agent(
                conn,
                int(agent["id"]),
                current_state=state_name,
                state_entered_at=now,
                ready_at="",
                ended_at=now,
                phase="finished",
                status_message=reason,
            )
            self.backend.terminate(agent, immediate=False)
            return
        ready_at = ""
        phase = "enter_state"
        if wait_text:
            ready_at = format_utc(now_dt + timedelta(seconds=parse_wait_seconds(wait_text)))
            phase = "waiting"
        update_agent(
            conn,
            int(agent["id"]),
            current_state=state_name,
            state_entered_at=now,
            ready_at=ready_at,
            phase=phase,
            status_message=reason if not ready_at else f"Waiting until {ready_at}",
            pending_state_json="",
            current_turn_id="",
            current_turn_kind="",
            current_turn_started_at="",
        )
        if ready_at:
            record_agent_event(
                conn,
                int(agent["id"]),
                "delay",
                state_name=state_name,
                reason=f"Waiting for {wait_text}",
                payload={"wait": wait_text, "ready_at": ready_at},
            )
        if not ready_at:
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
            ready_at="",
            status_message=reason,
        )
        self.backend.terminate(agent, immediate=False)

    def _agent_flow(self, conn: Any, agent: dict[str, Any]) -> FlowSpec:
        snapshot = get_flow_snapshot(conn, int(agent["flow_snapshot_id"]))
        payload = json.loads(snapshot["snapshot_json"])
        return flow_from_dict(payload)

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
        if observation.status != "completed":
            return
        status_message = "Needs help" if agent["substate"] == "needs_help" else "Paused in interaction"
        update_agent(
            conn,
            int(agent["id"]),
            current_turn_id="",
            current_turn_kind="",
            current_turn_started_at="",
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


def build_state_prompt(flow: FlowSpec, state: StateSpec, agent: dict[str, Any]) -> str:
    return _control_wrapped_prompt(
        agent,
        "state_prompt",
        "\n".join(
            [
                f"Flow: {flow.name}",
                f"State: {state.name}",
                "",
                "Work on the following state instructions:",
                state.prompt.strip(),
            ]
        ).strip(),
    )


def build_continue_prompt(flow: FlowSpec, state: StateSpec, agent: dict[str, Any]) -> str:
    return _control_wrapped_prompt(
        agent,
        "continue_prompt",
        "\n".join(
            [
                f"Continue working in state '{state.name}'.",
                "Use your best judgement and keep pushing the current state forward.",
                "Do not evaluate transitions yet; keep working until the runtime asks again.",
            ]
        ),
    )


def build_transition_prompt(flow: FlowSpec, state: StateSpec, agent: dict[str, Any], *, allow_keep_working: bool) -> str:
    lines = [
        f"You are evaluating outgoing transitions for flow '{flow.name}' state '{state.name}'.",
        "Choose exactly one transition name.",
        "",
        "Explicit transitions:",
    ]
    for transition in state.transitions:
        condition = transition.condition or "(unconditional)"
        wait_suffix = f" [wait {transition.wait}]" if transition.wait else ""
        lines.append(f"- {transition.target}: {condition}{wait_suffix}")
    lines.extend(
        [
            "",
            "Implicit transitions:",
            f"- {IMPLICIT_TRANSITION_NEEDS_HELP}: choose this if you are blocked or need human input.",
        ]
    )
    if allow_keep_working:
        lines.append(
            f"- {IMPLICIT_TRANSITION_KEEP_WORKING}: choose this if more work in the current state is the best action."
        )
    lines.extend(
        [
            "",
            'Respond with JSON only in the form {"choice": "<name>", "reason": "<short explanation>"}',
        ]
    )
    return _control_wrapped_prompt(agent, "transition_eval", "\n".join(lines))


def parse_decision(text: str) -> Decision:
    raw = text.strip()
    if raw.startswith("```"):
        parts = raw.splitlines()
        if len(parts) >= 3 and parts[-1].strip() == "```":
            raw = "\n".join(parts[1:-1]).strip()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("transition evaluation did not return a JSON object")
    choice = payload.get("choice")
    reason = payload.get("reason") or ""
    if not isinstance(choice, str) or not choice.strip():
        raise ValueError("transition evaluation JSON is missing 'choice'")
    if not isinstance(reason, str):
        raise ValueError("transition evaluation JSON has non-string 'reason'")
    return Decision(choice=choice.strip(), reason=reason.strip(), raw_json=raw)


def _control_wrapped_prompt(agent: dict[str, Any], kind: str, body: str) -> str:
    marker = agent.get("launch_marker") or f"flow-{agent['id']}-{uuid.uuid4().hex[:8]}"
    return "\n".join(
        [
            "[flow-control]",
            f"agent_id: {agent['id']}",
            f"marker: {marker}",
            f"kind: {kind}",
            "[/flow-control]",
            "",
            body.strip(),
        ]
    )


def _selected_transition(state: StateSpec, choice: str) -> TransitionSpec | None:
    for transition in state.transitions:
        if transition.target == choice:
            return transition
    return None


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
