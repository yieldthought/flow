"""Prompt construction and structured transition parsing."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from .constants import IMPLICIT_FINISH, IMPLICIT_KEEP_WORKING, IMPLICIT_NEEDS_HELP
from .spec import FlowSpec, StateSpec

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "request_id": {"type": "string"},
        "choice": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["request_id", "choice", "reason"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Decision:
    choice: str
    reason: str
    request_id: str


def state_prompt(flow: FlowSpec, state: StateSpec, scratchpad: str) -> str:
    return (
        f"Flow: {flow.name}\n"
        f"State: {state.name}\n"
        f"Scratchpad: {scratchpad}\n\n"
        "Work on this state's instructions. Keep durable context in the scratchpad body; do not edit its "
        "managed metadata header. Do not decide the next workflow state yet.\n\n"
        f"{state.prompt.strip()}"
    ).strip()


def continue_prompt(flow: FlowSpec, state: StateSpec, scratchpad: str) -> str:
    return (
        f"Flow: {flow.name}\n"
        f"State: {state.name}\n"
        f"Scratchpad: {scratchpad}\n\n"
        "Continue the current state's work using the thread history and scratchpad. A previous turn may have "
        "been interrupted or a human may have intervened. Do not evaluate workflow transitions yet."
    )


def decision_prompt(flow: FlowSpec, state: StateSpec, scratchpad: str) -> tuple[str, str]:
    request_id = str(uuid.uuid4())
    lines = [
        f"Evaluate Flow '{flow.name}' state '{state.name}'.",
        f"Scratchpad: {scratchpad}",
        "Choose exactly one action using the requested JSON schema.",
        "",
    ]
    if state.terminal:
        lines.extend(
            [
                "Actions:",
                f"- {IMPLICIT_FINISH}: this terminal state's work is complete.",
                f"- {IMPLICIT_KEEP_WORKING}: more work in this terminal state is useful.",
                f"- {IMPLICIT_NEEDS_HELP}: human input or intervention is required.",
            ]
        )
    else:
        lines.append("Explicit transitions:")
        for transition in state.transitions:
            condition = transition.condition or "unconditional"
            wait = f" (wait {transition.wait})" if transition.wait else ""
            lines.append(f"- {transition.target}: {condition}{wait}")
        lines.extend(
            [
                "",
                "Implicit actions:",
                f"- {IMPLICIT_KEEP_WORKING}: more work in the current state is useful.",
                f"- {IMPLICIT_NEEDS_HELP}: human input or intervention is required.",
            ]
        )
    lines.extend(["", f"Set request_id to exactly: {request_id}"])
    return "\n".join(lines), request_id


def parse_decision(text: str, request_id: str) -> Decision:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Codex returned invalid transition JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Codex transition response must be a JSON object")
    response_id = str(payload.get("request_id") or "")
    if response_id != request_id:
        raise ValueError("Codex transition response has the wrong request_id")
    choice = str(payload.get("choice") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    if not choice or not reason:
        raise ValueError("Codex transition response must include choice and reason")
    return Decision(choice=choice, reason=reason, request_id=response_id)
