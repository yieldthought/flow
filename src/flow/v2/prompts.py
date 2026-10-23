"""Prompt construction and structured transition parsing."""

from __future__ import annotations

import json
from dataclasses import dataclass

from .constants import IMPLICIT_FINISH, IMPLICIT_KEEP_WORKING, IMPLICIT_NEEDS_HELP
from .spec import FlowSpec, StateSpec

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "choice": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["choice", "reason"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Decision:
    choice: str
    reason: str


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


def decision_prompt(flow: FlowSpec, state: StateSpec, scratchpad: str) -> str:
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
    return "\n".join(lines)


def parse_decision(text: str) -> Decision:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Codex returned invalid transition JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Codex transition response must be a JSON object")
    # Older checkpoints can recover responses containing request_id. The SDK
    # turn handle (or recorded turn ID on resume) already identifies the response.
    choice = payload.get("choice")
    reason = payload.get("reason")
    if not isinstance(choice, str) or not isinstance(reason, str) or not choice.strip() or not reason.strip():
        raise ValueError("Codex transition response must include non-empty choice and reason strings")
    return Decision(choice=choice.strip(), reason=reason.strip())
