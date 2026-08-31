"""Parsing, validation, argument handling, and rendering for `.flow` files."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from flow.common import canonical_cli_name, parse_wait_seconds

from .constants import (
    FLOW_EXIT_MAX,
    FLOW_EXIT_MIN,
    RESERVED_STATE_NAMES,
    SCHEMA_VERSION,
    VALID_MODES,
    VALID_THINKING,
)
from .style import StyledArgumentParser

PLACEHOLDER_RE = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")


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
    exit_code: int | None = None
    prompt: str = ""
    wait: str | None = None
    mode: str | None = None
    thinking: str | None = None
    fast: bool | None = None
    transitions: tuple[TransitionSpec, ...] = ()

    @property
    def terminal(self) -> bool:
        return self.exit_code is not None


@dataclass(frozen=True)
class FlowSpec:
    name: str
    description: str | None
    version: int
    path: str | None
    mode: str
    thinking: str
    fast: bool
    model: str | None
    args: dict[str, ArgSpec]
    states: dict[str, StateSpec]
    source_path: str
    digest: str
    placeholders: tuple[str, ...] = field(default_factory=tuple)

    @property
    def start_state(self) -> str:
        starts = [state.name for state in self.states.values() if state.start]
        if len(starts) != 1:
            raise ValueError("flow must define exactly one start state")
        return starts[0]


@dataclass(frozen=True)
class ValidationResult:
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class CatalogEntry:
    path: str
    flow: FlowSpec | None
    validation: ValidationResult | None
    error: str = ""


def load_flow(path: str | Path) -> FlowSpec:
    source = Path(path).expanduser().resolve()
    raw = source.read_bytes()
    data = yaml.safe_load(raw.decode("utf-8")) or {}
    return flow_from_mapping(
        data,
        source_path=str(source),
        digest=hashlib.sha256(raw).hexdigest(),
    )


def discover_catalog(paths: list[str | Path] | None = None) -> tuple[CatalogEntry, ...]:
    roots = [Path(item).expanduser().resolve() for item in paths] if paths else _catalog_roots()
    files: set[Path] = set()
    for root in roots:
        if root.is_file() and root.suffix == ".flow":
            files.add(root)
        elif root.is_dir():
            files.update(root.rglob("*.flow"))
    entries: list[CatalogEntry] = []
    for path in sorted(files):
        try:
            flow = load_flow(path)
            entries.append(CatalogEntry(str(path), flow, validate_flow(flow)))
        except Exception as exc:
            entries.append(CatalogEntry(str(path), None, None, str(exc)))
    return tuple(entries)


def flow_from_mapping(data: Any, *, source_path: str = "", digest: str = "") -> FlowSpec:
    if not isinstance(data, dict):
        raise ValueError("flow file must contain a mapping at the top level")
    block = data.get("flow")
    if not isinstance(block, dict):
        raise ValueError("flow file must contain a top-level 'flow' mapping")

    name = block.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("flow.name must be a non-empty string")
    version = block.get("version", SCHEMA_VERSION)
    if not isinstance(version, int):
        raise ValueError("flow.version must be an integer")
    description = block.get("description")
    if description is not None and not isinstance(description, str):
        raise ValueError("flow.description must be a string")
    path_value = block.get("path")
    if path_value is not None and not isinstance(path_value, str):
        raise ValueError("flow.path must be a string")
    mode = block.get("mode", "yolo")
    thinking = block.get("thinking", "xhigh")
    fast = block.get("fast", False)
    model = block.get("model")
    if mode not in VALID_MODES:
        raise ValueError(f"invalid flow mode '{mode}'")
    if thinking not in VALID_THINKING:
        raise ValueError(f"invalid flow thinking '{thinking}'")
    if not isinstance(fast, bool):
        raise ValueError("flow.fast must be a boolean")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ValueError("flow.model must be a non-empty string")

    states: dict[str, StateSpec] = {}
    for state_name, raw_state in data.items():
        if state_name == "flow":
            continue
        if not isinstance(state_name, str) or not isinstance(raw_state, dict):
            raise ValueError(f"state '{state_name}' must be a mapping")
        states[state_name] = _parse_state(state_name, raw_state)

    args = _parse_args(block.get("args") or {})
    placeholders = tuple(sorted(_discover_placeholders(data)))
    return FlowSpec(
        name=name.strip(),
        description=description.strip() if isinstance(description, str) and description.strip() else None,
        version=version,
        path=path_value,
        mode=mode,
        thinking=thinking,
        fast=fast,
        model=model.strip() if isinstance(model, str) else None,
        args=args,
        states=states,
        source_path=source_path,
        digest=digest,
        placeholders=placeholders,
    )


def validate_flow(flow: FlowSpec) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    if flow.version != SCHEMA_VERSION:
        errors.append(f"flow.version must be {SCHEMA_VERSION}")
    if not flow.states:
        errors.append("flow must define at least one state")
    starts = [state.name for state in flow.states.values() if state.start]
    if len(starts) != 1:
        errors.append(f"flow must define exactly one start state (found {len(starts)})")

    for name in sorted(set(flow.placeholders) - set(flow.args)):
        errors.append(f"placeholder '{{{{{name}}}}}' is used but not declared in flow.args")

    for state in flow.states.values():
        if state.name in RESERVED_STATE_NAMES:
            errors.append(f"state '{state.name}' uses a reserved implicit action name")
        if state.terminal and state.transitions:
            errors.append(f"terminal state '{state.name}' cannot define transitions")
        if not state.terminal and not state.transitions:
            errors.append(f"state '{state.name}' must define transitions or an exit code")
        if not state.terminal and not state.prompt.strip():
            errors.append(f"non-terminal state '{state.name}' must define a prompt")
        if state.exit_code is not None and not FLOW_EXIT_MIN <= state.exit_code <= FLOW_EXIT_MAX:
            errors.append(
                f"terminal state '{state.name}' exit code must be between {FLOW_EXIT_MIN} and {FLOW_EXIT_MAX}"
            )
        _validate_wait(state.wait, f"state '{state.name}'", errors)
        unconditional = 0
        for index, transition in enumerate(state.transitions):
            if transition.target not in flow.states:
                errors.append(
                    f"state '{state.name}' transition {index + 1} targets unknown state '{transition.target}'"
                )
            _validate_wait(transition.wait, f"state '{state.name}' transition {index + 1}", errors)
            if not transition.condition:
                unconditional += 1
                if index != len(state.transitions) - 1:
                    errors.append(
                        f"state '{state.name}' has an unconditional transition before the end of the list"
                    )
        if unconditional > 1:
            errors.append(f"state '{state.name}' defines more than one unconditional transition")
        if state.mode is not None and state.mode not in VALID_MODES:
            errors.append(f"state '{state.name}' has invalid mode '{state.mode}'")
        if state.thinking is not None and state.thinking not in VALID_THINKING:
            errors.append(f"state '{state.name}' has invalid thinking '{state.thinking}'")

    reachable = _reachable(flow)
    if reachable and not any(flow.states[name].terminal for name in reachable):
        errors.append("flow must have at least one reachable terminal state")
    for name in flow.states:
        if name not in reachable:
            warnings.append(f"state '{name}' is unreachable from the start state")
    for name in sorted(set(flow.args) - set(flow.placeholders)):
        warnings.append(f"flow arg '{name}' is declared but never referenced")
    return ValidationResult(tuple(errors), tuple(warnings))


def parse_arguments(
    flow: FlowSpec,
    argv: list[str],
    invocation_cwd: str,
    *,
    colour_output: bool = True,
) -> tuple[dict[str, str], str]:
    parser = StyledArgumentParser(
        prog=Path(flow.source_path).name,
        description=flow.description,
        add_help=True,
        colour=colour_output,
    )
    names = sorted(set(flow.placeholders) | set(flow.args))
    for name in names:
        spec = flow.args.get(name, ArgSpec(name))
        options: dict[str, Any] = {
            "dest": name,
            "help": spec.help or argparse.SUPPRESS,
            "default": argparse.SUPPRESS,
        }
        if spec.default is not None:
            options["default"] = spec.default
        parser.add_argument(f"--{canonical_cli_name(name)}", **options)
    parser.add_argument("--path", dest="__path__", default=argparse.SUPPRESS)
    parsed = parser.parse_args(argv)

    missing = object()
    values: dict[str, str] = {}
    for name in names:
        value = getattr(parsed, name, missing)
        if value is missing:
            raise ValueError(f"missing required argument '--{canonical_cli_name(name)}'")
        values[name] = str(value)

    path_value = getattr(parsed, "__path__", missing)
    if path_value is missing:
        path_value = _render(flow.path, values) if flow.path else invocation_cwd
    path = Path(str(path_value)).expanduser()
    if not path.is_absolute():
        path = Path(invocation_cwd) / path
    return values, str(path.resolve())


def render_flow(flow: FlowSpec, values: dict[str, str], cwd: str) -> FlowSpec:
    rendered_states = {
        name: replace(
            state,
            prompt=_render(state.prompt, values),
            wait=_render_optional(state.wait, values),
            transitions=tuple(
                replace(
                    transition,
                    condition=_render_optional(transition.condition, values),
                    wait=_render_optional(transition.wait, values),
                )
                for transition in state.transitions
            ),
        )
        for name, state in flow.states.items()
    }
    return replace(
        flow,
        name=_render(flow.name, values),
        description=_render_optional(flow.description, values),
        path=cwd,
        states=rendered_states,
    )


def _parse_state(name: str, raw: dict[str, Any]) -> StateSpec:
    start = raw.get("start", False)
    if not isinstance(start, bool):
        raise ValueError(f"state '{name}' start must be a boolean")
    if "end" in raw:
        raise ValueError(f"state '{name}' uses V1 'end'; use 'exit: N' in Flow 2.0")
    exit_code = raw.get("exit")
    if exit_code is not None and (not isinstance(exit_code, int) or isinstance(exit_code, bool)):
        raise ValueError(f"state '{name}' exit must be an integer")
    prompt = raw.get("prompt", "")
    if not isinstance(prompt, str):
        raise ValueError(f"state '{name}' prompt must be a string")
    wait = raw.get("wait")
    if wait is not None and not isinstance(wait, (str, int)):
        raise ValueError(f"state '{name}' wait must be a duration string")
    mode = raw.get("mode")
    thinking = raw.get("thinking")
    if mode is not None and not isinstance(mode, str):
        raise ValueError(f"state '{name}' mode must be a string")
    if thinking is not None and not isinstance(thinking, str):
        raise ValueError(f"state '{name}' thinking must be a string")
    fast = raw.get("fast")
    if fast is not None and not isinstance(fast, bool):
        raise ValueError(f"state '{name}' fast must be a boolean")
    transitions_raw = raw.get("transitions") or []
    if not isinstance(transitions_raw, list):
        raise ValueError(f"state '{name}' transitions must be a list")
    transitions: list[TransitionSpec] = []
    for index, item in enumerate(transitions_raw):
        if not isinstance(item, dict):
            raise ValueError(f"state '{name}' transition {index + 1} must be a mapping")
        target = item.get("go")
        if not isinstance(target, str) or not target.strip():
            raise ValueError(f"state '{name}' transition {index + 1} must define 'go'")
        condition = item.get("if")
        if condition is not None and not isinstance(condition, str):
            raise ValueError(f"state '{name}' transition {index + 1} if must be a string")
        transition_wait = item.get("wait")
        if transition_wait is not None and not isinstance(transition_wait, (str, int)):
            raise ValueError(f"state '{name}' transition {index + 1} wait must be a duration string")
        transitions.append(
            TransitionSpec(
                target=target.strip(),
                condition=condition.strip() if isinstance(condition, str) and condition.strip() else None,
                wait=str(transition_wait) if transition_wait is not None else None,
            )
        )
    return StateSpec(
        name=name,
        start=start,
        exit_code=exit_code,
        prompt=prompt,
        wait=str(wait) if wait is not None else None,
        mode=mode,
        thinking=thinking,
        fast=fast,
        transitions=tuple(transitions),
    )


def _parse_args(raw: Any) -> dict[str, ArgSpec]:
    if not isinstance(raw, dict):
        raise ValueError("flow.args must be a mapping")
    result: dict[str, ArgSpec] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name:
            raise ValueError("flow argument names must be non-empty strings")
        if value is None:
            value = {}
        if not isinstance(value, dict):
            raise ValueError(f"flow argument '{name}' must be a mapping")
        help_text = value.get("help", "")
        default = value.get("default")
        if not isinstance(help_text, str):
            raise ValueError(f"flow argument '{name}' help must be a string")
        result[name] = ArgSpec(name=name, help=help_text, default=None if default is None else str(default))
    return result


def _discover_placeholders(value: Any) -> set[str]:
    if isinstance(value, str):
        return set(PLACEHOLDER_RE.findall(value))
    if isinstance(value, dict):
        found: set[str] = set()
        for key, item in value.items():
            found.update(_discover_placeholders(key))
            found.update(_discover_placeholders(item))
        return found
    if isinstance(value, list):
        found = set()
        for item in value:
            found.update(_discover_placeholders(item))
        return found
    return set()


def _render(value: str, values: dict[str, str]) -> str:
    def replace_match(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise ValueError(f"missing value for placeholder '{{{{{name}}}}}'")
        return values[name]

    return PLACEHOLDER_RE.sub(replace_match, value)


def _render_optional(value: str | None, values: dict[str, str]) -> str | None:
    return _render(value, values) if value is not None else None


def _validate_wait(value: str | None, context: str, errors: list[str]) -> None:
    if value is None or PLACEHOLDER_RE.search(value):
        return
    try:
        parse_wait_seconds(value)
    except ValueError as exc:
        errors.append(f"{context} has {exc}")


def _reachable(flow: FlowSpec) -> set[str]:
    try:
        pending = [flow.start_state]
    except ValueError:
        return set()
    reached: set[str] = set()
    while pending:
        name = pending.pop()
        if name in reached or name not in flow.states:
            continue
        reached.add(name)
        pending.extend(transition.target for transition in flow.states[name].transitions)
    return reached


def _catalog_roots() -> list[Path]:
    raw = os.environ.get("FLOW_PATH", "").strip()
    values = raw.split(os.pathsep) if raw else ["~/flows", "./flows"]
    return [Path(value).expanduser().resolve() for value in values if value.strip()]
