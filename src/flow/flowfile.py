"""Flow YAML parsing, rendering, and validation."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .common import (
    DEFAULT_MODE,
    DEFAULT_THINKING,
    PLACEHOLDER_RE,
    RESERVED_STATE_NAMES,
    VALID_MODES,
    VALID_THINKING,
    canonical_cli_name,
    expand_path,
    parse_wait_seconds,
)


@dataclass(frozen=True)
class ArgSpec:
    name: str
    help: str = ""
    default: str | None = None


@dataclass(frozen=True)
class TransitionSpec:
    target: str
    condition: str | None = None
    wait: str | None = None


@dataclass(frozen=True)
class StateSpec:
    name: str
    start: bool = False
    end: bool = False
    prompt: str = ""
    wait: str | None = None
    mode: str | None = None
    thinking: str | None = None
    transitions: tuple[TransitionSpec, ...] = ()


@dataclass(frozen=True)
class FlowSpec:
    name: str
    version: int
    path: str | None
    mode: str | None
    thinking: str | None
    args: dict[str, ArgSpec]
    states: dict[str, StateSpec]
    source_path: str
    placeholders: tuple[str, ...] = field(default_factory=tuple)

    @property
    def start_states(self) -> list[str]:
        return [state.name for state in self.states.values() if state.start]

    @property
    def end_states(self) -> list[str]:
        return [state.name for state in self.states.values() if state.end]


@dataclass(frozen=True)
class ValidationResult:
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


def load_flow(path: str | Path) -> FlowSpec:
    source = Path(path).expanduser().resolve()
    data = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("flow file must contain a mapping at the top level")
    flow_block = data.get("flow")
    if not isinstance(flow_block, dict):
        raise ValueError("flow file must contain a top-level 'flow' mapping")

    states: dict[str, StateSpec] = {}
    for name, raw in data.items():
        if name == "flow":
            continue
        if not isinstance(raw, dict):
            raise ValueError(f"state '{name}' must be a mapping")
        states[name] = _parse_state(name, raw)

    args = _parse_args(flow_block.get("args") or {})
    placeholders = tuple(sorted(_discover_placeholders(data)))
    version = flow_block.get("version", 1)
    if not isinstance(version, int):
        raise ValueError("flow.version must be an integer when provided")
    name = flow_block.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("flow.name must be a non-empty string")
    path_value = flow_block.get("path")
    if path_value is not None and not isinstance(path_value, str):
        raise ValueError("flow.path must be a string when provided")
    mode = flow_block.get("mode")
    if mode is not None and mode not in VALID_MODES:
        raise ValueError(f"invalid flow mode '{mode}'")
    thinking = flow_block.get("thinking")
    if thinking is not None and thinking not in VALID_THINKING:
        raise ValueError(f"invalid flow thinking '{thinking}'")

    return FlowSpec(
        name=name.strip(),
        version=version,
        path=path_value,
        mode=mode,
        thinking=thinking,
        args=args,
        states=states,
        source_path=str(source),
        placeholders=placeholders,
    )


def validate_flow(flow: FlowSpec) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []

    if flow.version != 1:
        warnings.append(f"flow.version is {flow.version}; V1 runtime expects version 1")

    if not flow.states:
        errors.append("flow must define at least one state")

    if not flow.start_states:
        errors.append("flow must define at least one start state")

    referenced = set()
    for state in flow.states.values():
        if state.name in RESERVED_STATE_NAMES:
            errors.append(f"state '{state.name}' uses a reserved name")
        if state.start and state.end:
            errors.append(f"state '{state.name}' cannot be both start and end")
        if state.end and state.wait:
            errors.append(f"end state '{state.name}' cannot define wait")
        if not state.end and not state.transitions:
            errors.append(f"state '{state.name}' must define at least one transition")
        _validate_wait_literal(state.wait, f"state '{state.name}'", errors)
        unconditional_seen = 0
        for index, transition in enumerate(state.transitions):
            if transition.target not in flow.states:
                errors.append(
                    f"state '{state.name}' transition {index + 1} targets unknown state '{transition.target}'"
                )
            target_state = flow.states.get(transition.target)
            if transition.wait and target_state is not None and target_state.end:
                errors.append(
                    f"state '{state.name}' transition {index + 1} cannot wait before entering end state '{transition.target}'"
                )
            _validate_wait_literal(
                transition.wait,
                f"state '{state.name}' transition {index + 1}",
                errors,
            )
            referenced.add(transition.target)
            if not transition.condition:
                unconditional_seen += 1
                if index != len(state.transitions) - 1:
                    errors.append(
                        f"state '{state.name}' has an unconditional transition before the end of the list"
                    )
        if unconditional_seen > 1:
            errors.append(f"state '{state.name}' defines more than one unconditional transition")
        if state.mode is not None and state.mode not in VALID_MODES:
            errors.append(f"state '{state.name}' has invalid mode '{state.mode}'")
        if state.thinking is not None and state.thinking not in VALID_THINKING:
            errors.append(f"state '{state.name}' has invalid thinking '{state.thinking}'")

    unused_args = sorted(set(flow.args) - set(flow.placeholders))
    for name in unused_args:
        warnings.append(f"flow arg '{name}' is defined but not referenced by any placeholder")

    for name in flow.placeholders:
        if name == "path":
            warnings.append("placeholder '{{path}}' is not special; use top-level flow.path instead")

    reachable = _reachable_states(flow)
    for state_name in flow.states:
        if state_name not in reachable:
            warnings.append(f"state '{state_name}' is unreachable from any start state")

    return ValidationResult(tuple(errors), tuple(warnings))


def render_flow(flow: FlowSpec, values: dict[str, str], cwd_override: str | None = None) -> FlowSpec:
    args = {name: ArgSpec(spec.name, spec.help, values.get(name, spec.default)) for name, spec in flow.args.items()}
    rendered_states = {
        name: StateSpec(
            name=state.name,
            start=state.start,
            end=state.end,
            prompt=_render_string(state.prompt, values),
            wait=_render_wait_string(state.wait, values),
            mode=state.mode,
            thinking=state.thinking,
            transitions=tuple(
                TransitionSpec(
                    target=transition.target,
                    condition=_render_string(transition.condition, values) if transition.condition else None,
                    wait=_render_wait_string(transition.wait, values),
                )
                for transition in state.transitions
            ),
        )
        for name, state in flow.states.items()
    }
    rendered_path = cwd_override or (_render_string(flow.path, values) if flow.path else None)
    if rendered_path:
        rendered_path = expand_path(rendered_path)
    return FlowSpec(
        name=_render_string(flow.name, values),
        version=flow.version,
        path=rendered_path,
        mode=flow.mode or DEFAULT_MODE,
        thinking=flow.thinking or DEFAULT_THINKING,
        args=args,
        states=rendered_states,
        source_path=flow.source_path,
        placeholders=flow.placeholders,
    )


def parse_start_arguments(flow: FlowSpec, state_token: str | None, argv: list[str]) -> tuple[str, dict[str, str], str]:
    selected_state, remainder = _resolve_state_token(flow, state_token, argv)
    parser = argparse.ArgumentParser(prog="flow start", add_help=False)

    arg_names = sorted(set(flow.placeholders) | set(flow.args))
    for name in arg_names:
        spec = flow.args.get(name, ArgSpec(name))
        flag = f"--{canonical_cli_name(name)}"
        kwargs: dict[str, Any] = {"dest": name, "help": spec.help or argparse.SUPPRESS}
        if spec.default is not None:
            kwargs["default"] = str(spec.default)
        parser.add_argument(flag, **kwargs)

    parser.add_argument("--path", dest="__path__", default=None, metavar="PATH")
    parser.add_argument("--help", action="help")

    parsed = parser.parse_args(remainder)
    values: dict[str, str] = {}
    for name in arg_names:
        value = getattr(parsed, name)
        if value is None:
            raise ValueError(f"missing required argument '--{canonical_cli_name(name)}' for placeholder '{{{{{name}}}}}'")
        values[name] = str(value)

    path_value = parsed.__path__
    if path_value is None and flow.path:
        path_value = _render_string(flow.path, values)
    if path_value is None:
        path_value = str(Path.cwd())
    return selected_state, values, expand_path(path_value)


def flow_to_dict(flow: FlowSpec) -> dict[str, Any]:
    return {
        "name": flow.name,
        "version": flow.version,
        "path": flow.path,
        "mode": flow.mode,
        "thinking": flow.thinking,
        "args": {
            name: {"name": spec.name, "help": spec.help, "default": spec.default}
            for name, spec in flow.args.items()
        },
        "states": {
            name: {
                "name": state.name,
                "start": state.start,
                "end": state.end,
                "prompt": state.prompt,
                "wait": state.wait,
                "mode": state.mode,
                "thinking": state.thinking,
                "transitions": [
                    {"target": transition.target, "condition": transition.condition, "wait": transition.wait}
                    for transition in state.transitions
                ],
            }
            for name, state in flow.states.items()
        },
        "source_path": flow.source_path,
        "placeholders": list(flow.placeholders),
    }


def flow_from_dict(payload: dict[str, Any]) -> FlowSpec:
    return FlowSpec(
        name=str(payload["name"]),
        version=int(payload["version"]),
        path=payload.get("path"),
        mode=payload.get("mode"),
        thinking=payload.get("thinking"),
        args={
            name: ArgSpec(
                name=str(spec.get("name") or name),
                help=str(spec.get("help") or ""),
                default=None if spec.get("default") is None else str(spec.get("default")),
            )
            for name, spec in dict(payload.get("args") or {}).items()
        },
        states={
            name: StateSpec(
                name=str(state.get("name") or name),
                start=bool(state.get("start")),
                end=bool(state.get("end")),
                prompt=str(state.get("prompt") or ""),
                wait=None if state.get("wait") is None else str(state.get("wait")),
                mode=state.get("mode"),
                thinking=state.get("thinking"),
                transitions=tuple(
                    TransitionSpec(
                        target=str(item["target"]),
                        condition=None if item.get("condition") is None else str(item.get("condition")),
                        wait=None if item.get("wait") is None else str(item.get("wait")),
                    )
                    for item in list(state.get("transitions") or [])
                ),
            )
            for name, state in dict(payload.get("states") or {}).items()
        },
        source_path=str(payload.get("source_path") or ""),
        placeholders=tuple(str(item) for item in list(payload.get("placeholders") or [])),
    )


def _resolve_state_token(flow: FlowSpec, state_token: str | None, argv: list[str]) -> tuple[str, list[str]]:
    start_states = flow.start_states
    if state_token and not state_token.startswith("-"):
        if state_token not in start_states:
            raise ValueError(
                f"'{state_token}' is not a start state; expected one of: {', '.join(sorted(start_states))}"
            )
        return state_token, argv
    if len(start_states) == 1:
        return start_states[0], [state_token, *argv] if state_token else list(argv)
    raise ValueError(f"multiple start states defined; choose one of: {', '.join(sorted(start_states))}")


def _parse_args(raw: dict[str, Any]) -> dict[str, ArgSpec]:
    if not isinstance(raw, dict):
        raise ValueError("flow.args must be a mapping")
    args: dict[str, ArgSpec] = {}
    for name, spec in raw.items():
        if isinstance(spec, dict):
            help_text = spec.get("help") or ""
            default = spec.get("default")
        else:
            help_text = ""
            default = spec
        if default is not None and not isinstance(default, (str, int, float, bool)):
            raise ValueError(f"flow arg '{name}' default must be a scalar")
        args[name] = ArgSpec(name=name, help=str(help_text), default=None if default is None else str(default))
    return args


def _parse_state(name: str, raw: dict[str, Any]) -> StateSpec:
    transitions_raw = raw.get("transitions") or []
    if not isinstance(transitions_raw, list):
        raise ValueError(f"state '{name}' transitions must be a list")
    transitions: list[TransitionSpec] = []
    for item in transitions_raw:
        if not isinstance(item, dict):
            raise ValueError(f"state '{name}' transitions must contain mappings")
        target = item.get("go")
        if not isinstance(target, str) or not target.strip():
            raise ValueError(f"state '{name}' has a transition without a valid 'go' target")
        condition = item.get("if")
        if condition is not None and not isinstance(condition, str):
            raise ValueError(f"state '{name}' transition to '{target}' has a non-string 'if' condition")
        wait = item.get("wait")
        if wait is not None and not isinstance(wait, str):
            raise ValueError(f"state '{name}' transition to '{target}' has a non-string 'wait' value")
        transitions.append(
            TransitionSpec(
                target=target.strip(),
                condition=condition.strip() if isinstance(condition, str) and condition.strip() else None,
                wait=wait.strip() if isinstance(wait, str) and wait.strip() else None,
            )
        )
    prompt = raw.get("prompt") or ""
    if prompt is None:
        prompt = ""
    if not isinstance(prompt, str):
        raise ValueError(f"state '{name}' prompt must be a string")
    wait = raw.get("wait")
    if wait is not None and not isinstance(wait, str):
        raise ValueError(f"state '{name}' wait must be a string like '10m'")
    mode = raw.get("mode")
    thinking = raw.get("thinking")
    return StateSpec(
        name=name,
        start=bool(raw.get("start")),
        end=bool(raw.get("end")),
        prompt=prompt,
        wait=wait.strip() if isinstance(wait, str) and wait.strip() else None,
        mode=mode,
        thinking=thinking,
        transitions=tuple(transitions),
    )


def _discover_placeholders(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str):
        found.update(PLACEHOLDER_RE.findall(value))
    elif isinstance(value, dict):
        for item in value.values():
            found.update(_discover_placeholders(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_discover_placeholders(item))
    return found


def _render_string(value: str | None, values: dict[str, str]) -> str:
    if not value:
        return ""

    def replace(match: Any) -> str:
        key = match.group(1)
        if key not in values:
            raise ValueError(f"missing value for placeholder '{{{{{key}}}}}'")
        return str(values[key])

    return PLACEHOLDER_RE.sub(replace, value)


def _render_wait_string(value: str | None, values: dict[str, str]) -> str | None:
    if value is None:
        return None
    rendered = _render_string(value, values).strip()
    if not rendered:
        return None
    parse_wait_seconds(rendered)
    return rendered


def _reachable_states(flow: FlowSpec) -> set[str]:
    pending = list(flow.start_states)
    visited: set[str] = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        state = flow.states.get(current)
        if state is None:
            continue
        for transition in state.transitions:
            if transition.target not in flow.states:
                continue
            if transition.target not in visited:
                pending.append(transition.target)
    return visited


def _validate_wait_literal(value: str | None, context: str, errors: list[str]) -> None:
    if value is None:
        return
    if PLACEHOLDER_RE.search(value):
        return
    try:
        parse_wait_seconds(value)
    except ValueError as exc:
        errors.append(f"{context} has invalid wait '{value}': {exc}")
