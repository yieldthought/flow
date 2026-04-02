"""CLI rendering helpers."""

from __future__ import annotations

import ast
import json
import re
from collections import defaultdict
from datetime import datetime
from typing import Any

from .ansi import PALETTE, bold, color
from .common import duration_seconds, normalize_phase, parse_utc, utc_now
from .flowfile import flow_from_dict
from .store import (
    cumulative_agent_seconds,
    daemon_exit_info,
    daemon_status,
    get_flow_snapshot,
    get_meta,
    list_daemon_events,
    list_error_events,
    total_active_seconds,
    total_agent_count,
)

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def render_list(conn: Any, agents: list[dict[str, Any]]) -> str:
    status = daemon_status(conn)
    active = status["active"] == "1"
    uptime = _format_seconds(duration_seconds(status["started_at"])) if active else "-"
    active_agents = sum(1 for agent in agents if not agent["ended_at"])
    total_agents = total_agent_count(conn)
    cumulative = _format_seconds(cumulative_agent_seconds(conn))
    first_line = " | ".join(
        [
            f"Runtime {color('active', PALETTE.ok, bold=True) if active else color('shut down', PALETTE.error, bold=True)}",
            f"uptime {uptime}",
            f"active agents {active_agents}",
            f"total agents {total_agents}",
            f"cumulative agent time {cumulative}",
        ]
    )
    lines = [first_line]
    issues = _render_list_issues(conn, daemon_active=active)
    if issues:
        lines.extend(["", bold(color("Diagnostics", PALETTE.warn, bold=True)), *issues])
    if not agents:
        lines.append("")
        lines.append("No agents.")
        return "\n".join(lines)

    grouped: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for agent in agents:
        grouped[agent["flow_name"]][agent["current_state"]].append(agent)
    id_width, status_width = _list_column_widths(conn, agents)
    end_state_map = _list_end_state_map(conn, agents)

    for flow_name in sorted(grouped):
        lines.extend(["", bold(color(flow_name, PALETTE.bright, bold=True))])
        state_names = sorted(grouped[flow_name], key=lambda name: (end_state_map.get((flow_name, name), False), name))
        for state_name in state_names:
            state_color = PALETTE.dim if end_state_map.get((flow_name, state_name), False) else PALETTE.state
            lines.append(f"  {bold(color(state_name, state_color, bold=True))}")
            state_agents = grouped[flow_name][state_name]
            state_agents.sort(key=lambda item: (item["substate"] == "needs_help", item["substate"] == "interaction", int(item["id"])))
            for agent in state_agents:
                lines.append("    " + _render_agent(conn, agent, id_width=id_width, status_width=status_width))
    return "\n".join(lines)


def render_show(conn: Any, agent: dict[str, Any], events: list[dict[str, Any]]) -> str:
    running_seconds = total_active_seconds(conn, int(agent["id"]))
    waiting_seconds = _total_waiting_seconds(events, agent)
    pad_day = _show_uses_day_padding(agent, events)
    state_width = max((_event_state_width(event) for event in events), default=0)

    started_text = _format_show_timestamp(agent["created_at"], pad_day=pad_day)
    header = (
        f"{bold(color(agent['flow_name'], PALETTE.bright, bold=True))} in "
        f"{color(agent['cwd'], PALETTE.subtle)} "
        f"({color('started', PALETTE.muted)} {started_text} | "
        f"{color(_format_compact_duration(running_seconds), PALETTE.ok, bold=True)} running, "
        f"{color(_format_compact_duration(waiting_seconds), PALETTE.warn, bold=True)} waiting)"
    )

    lines = [header]
    lines.append(
        " | ".join(
            [
                f"{color('State', PALETTE.muted)} {bold(color(agent['current_state'], PALETTE.state, bold=True))}",
                f"{color('Substate', PALETTE.muted)} {_format_substate(agent['substate'])}",
                f"{color('Phase', PALETTE.muted)} {color(normalize_phase(agent['phase']), PALETTE.accent)}",
            ]
        )
    )
    if agent.get("status_message"):
        lines.append(f"{color('Status', PALETTE.muted)} {color(str(agent['status_message']), PALETTE.subtle)}")

    args_payload = _parse_args_payload(agent.get("args_json", ""))
    for key, value in sorted(args_payload.items()):
        lines.append(f"{color(key + ':', PALETTE.accent, bold=True)} {value}")

    lines.append("")
    lines.append(bold(color("Events", PALETTE.bright, bold=True)))
    if not events:
        lines.append(color("No events yet.", PALETTE.subtle))
        return "\n".join(lines)

    relative_texts = [_format_show_relative_duration(_elapsed_since_start(agent["created_at"], event["created_at"])) for event in events]

    for event, relative_text in zip(events, relative_texts):
        absolute = _format_show_timestamp(event["created_at"], pad_day=pad_day)
        relative = color(f"({relative_text})", PALETTE.muted)
        lines.append(f"{absolute} {relative}: {_render_event(event, state_width=state_width, pad_day=pad_day)}")
    return "\n".join(lines)


def fit_list_top(text: str, height: int) -> str:
    lines = text.splitlines()
    if height <= 0:
        return ""
    if len(lines) <= height:
        return text
    if height == 1:
        return lines[0]
    shown = lines[: height - 1]
    remaining = len(lines) - len(shown)
    shown.append(color(f"... {remaining} more lines", PALETTE.muted))
    return "\n".join(shown)


def fit_show_top(text: str, height: int) -> str:
    lines = text.splitlines()
    if height <= 0:
        return ""
    if len(lines) <= height:
        return text

    header_end = next((index for index, line in enumerate(lines) if _strip_ansi(line).strip() == "Events"), -1)
    if header_end < 0:
        return "\n".join(lines[:height])

    header_lines = lines[: header_end + 1]
    body_lines = lines[header_end + 1 :]
    if len(header_lines) >= height:
        return "\n".join(header_lines[:height])
    slots = height - len(header_lines)
    if len(body_lines) <= slots:
        return text
    return "\n".join(header_lines + body_lines[-slots:])


def _render_agent(conn: Any, agent: dict[str, Any], *, id_width: int, status_width: int) -> str:
    agent_id, status_text, duration_text = _agent_display_fields(conn, agent)
    label = color(agent_id.ljust(id_width), PALETTE.accent, bold=True)
    path_text = color(agent["cwd"], PALETTE.subtle)
    args_text = color(_format_args(agent["args_json"]), PALETTE.muted)
    status_label = _color_status_label(status_text, status_width)
    return f"{label}  {status_label} {duration_text}  {path_text}  {args_text}"


def _list_column_widths(conn: Any, agents: list[dict[str, Any]]) -> tuple[int, int]:
    id_width = max((len(f"#{agent['id']}") for agent in agents), default=2)
    status_width = max((len(_agent_display_fields(conn, agent)[1]) for agent in agents), default=len("working"))
    return id_width, status_width


def _list_end_state_map(conn: Any, agents: list[dict[str, Any]]) -> dict[tuple[str, str], bool]:
    snapshots: dict[int, Any] = {}
    state_map: dict[tuple[str, str], bool] = {}
    for agent in agents:
        flow_name = str(agent["flow_name"])
        state_name = str(agent["current_state"])
        snapshot_id = int(agent["flow_snapshot_id"])
        flow = snapshots.get(snapshot_id)
        if flow is None:
            try:
                snapshot = get_flow_snapshot(conn, snapshot_id)
            except ValueError:
                state_map.setdefault((flow_name, state_name), False)
                continue
            flow = flow_from_dict(_parse_snapshot_payload(str(snapshot["snapshot_json"])))
            snapshots[snapshot_id] = flow
        state = flow.states.get(state_name)
        state_map[(flow_name, state_name)] = state_map.get((flow_name, state_name), False) or bool(state and state.end)
    return state_map


def _parse_snapshot_payload(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = ast.literal_eval(text)
    if not isinstance(payload, dict):
        raise ValueError("flow snapshot payload must decode to a mapping")
    return payload


def _agent_display_fields(conn: Any, agent: dict[str, Any]) -> tuple[str, str, str]:
    agent_id = f"#{agent['id']}"
    if agent["ended_at"]:
        return agent_id, "finished", _format_seconds(duration_seconds(agent["created_at"], agent["ended_at"]))
    if agent["substate"] == "needs_help":
        return agent_id, "needs help", _format_seconds(_state_seconds(conn, agent))
    if agent["substate"] == "interaction":
        return agent_id, "paused", _format_seconds(_state_seconds(conn, agent))
    waiting_seconds = _waiting_seconds(agent)
    if waiting_seconds > 0:
        return agent_id, "waiting", _format_seconds(waiting_seconds)
    return agent_id, "working", _format_seconds(_state_seconds(conn, agent))


def _color_status_label(status_text: str, status_width: int) -> str:
    code = _status_color(status_text)
    padding = " " * max(0, status_width - len(status_text))
    return color(status_text, code, bold=status_text == "needs help") + padding


def _status_color(status_text: str) -> int:
    if status_text == "waiting":
        return PALETTE.subtle
    if status_text == "paused":
        return PALETTE.warn
    if status_text == "needs help":
        return PALETTE.error
    if status_text == "working":
        return PALETTE.info
    if status_text == "finished":
        return PALETTE.muted
    return PALETTE.muted


def _format_args(text: str) -> str:
    payload = _parse_args_payload(text)
    if not payload:
        return "-"
    parts = [f"{key}={value}" for key, value in sorted(payload.items())]
    return " ".join(parts)


def _format_seconds(value: float) -> str:
    total = max(0, int(value))
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _state_seconds(conn: Any, agent: dict[str, Any]) -> float:
    from .store import state_active_seconds

    if agent["ended_at"]:
        return total_active_seconds(conn, int(agent["id"]))
    return state_active_seconds(conn, int(agent["id"]), agent["current_state"])


def _waiting_seconds(agent: dict[str, Any]) -> float:
    ready_at = parse_utc(agent.get("ready_at"))
    if ready_at is None:
        return 0.0
    return max(0.0, (ready_at - utc_now()).total_seconds())


def _parse_args_payload(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text or "{}")
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _format_compact_duration(value: float) -> str:
    total_minutes = max(0, int(value) // 60)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}h {minutes}m"


def _format_show_relative_duration(value: float) -> str:
    total_minutes = max(0, int(value) // 60)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}h {minutes:2d}m"


def _elapsed_since_start(started_at: str, event_at: str) -> float:
    start = parse_utc(started_at)
    event = parse_utc(event_at)
    if start is None or event is None:
        return 0.0
    return max(0.0, (event - start).total_seconds())


def _total_waiting_seconds(events: list[dict[str, Any]], agent: dict[str, Any]) -> float:
    cutoff = parse_utc(agent.get("ended_at")) or utc_now()
    total = 0.0
    for event in events:
        if event.get("kind") != "delay":
            continue
        started = parse_utc(event.get("created_at"))
        payload = _parse_args_payload(event.get("payload_json", ""))
        ready_at = parse_utc(str(payload.get("ready_at") or ""))
        if started is None or ready_at is None:
            continue
        finished = min(cutoff, ready_at)
        total += max(0.0, (finished - started).total_seconds())
    return total


def _render_event(event: dict[str, Any], *, state_width: int, pad_day: bool) -> str:
    kind = str(event.get("kind") or "")
    state_name = str(event.get("state_name") or "")
    from_state = str(event.get("from_state") or "")
    to_state = str(event.get("to_state") or "")
    choice = str(event.get("choice") or "")
    reason = str(event.get("reason") or "")
    payload = _parse_args_payload(event.get("payload_json", ""))

    if kind == "started":
        return f"{_render_state_column(state_name, state_width)}    {color('started', PALETTE.accent)}"
    if kind == "decision":
        source = _render_state_column(from_state or state_name or "state", state_width)
        target = to_state or choice or "decision"
        return f"{source} -> {bold(color(target, PALETTE.accent, bold=True))}{_quoted_reason(reason)}"
    if kind == "delay":
        wait_value = str(payload.get("wait") or "").strip() or "?"
        ready_at = str(payload.get("ready_at") or "")
        until_text = _format_show_timestamp(ready_at, pad_day=pad_day) if ready_at else "-"
        return f"{_render_state_column(state_name or from_state, state_width)}    {color('wait', PALETTE.warn, bold=True)} for {color(wait_value, PALETTE.warn, bold=True)} until {until_text}"
    if kind == "pause":
        return f"{_render_state_column(state_name or from_state, state_width)}    {color('paused', PALETTE.warn)}{_quoted_reason(reason)}"
    if kind == "interrupt":
        return f"{_render_state_column(state_name or from_state, state_width)}    {color('interrupted', PALETTE.warn)}{_quoted_reason(reason)}"
    if kind == "resume":
        return f"{_render_state_column(state_name or from_state, state_width)}    {color('resumed', PALETTE.ok)}{_quoted_reason(reason)}"
    if kind == "wake":
        return f"{_render_state_column(state_name or from_state, state_width)}    {color('woke', PALETTE.accent)}{_quoted_reason(reason)}"
    if kind == "needs_help":
        return f"{_render_state_column(state_name or from_state, state_width)}    {color('needs_help', PALETTE.error, bold=True)}{_quoted_reason(reason)}"
    label = kind or "event"
    return f"{color(label, PALETTE.accent)}{_quoted_reason(reason)}"


def _render_state_column(value: str, width: int) -> str:
    text = value or "state"
    padded = text.ljust(max(width, len(text)))
    return bold(color(padded, PALETTE.state, bold=True))


def _event_state_width(event: dict[str, Any]) -> int:
    kind = str(event.get("kind") or "")
    if kind == "decision":
        return len(str(event.get("from_state") or event.get("state_name") or "state"))
    if kind in {"started", "delay", "pause", "interrupt", "resume", "wake", "needs_help"}:
        return len(str(event.get("state_name") or event.get("from_state") or "state"))
    return 0


def _show_uses_day_padding(agent: dict[str, Any], events: list[dict[str, Any]]) -> bool:
    widths: set[int] = set()
    for value in _show_timestamp_values(agent, events):
        parsed = parse_utc(value)
        if parsed is None:
            continue
        widths.add(len(str(parsed.astimezone().day)))
    return widths == {1, 2}


def _show_timestamp_values(agent: dict[str, Any], events: list[dict[str, Any]]) -> list[str]:
    values = [str(agent.get("created_at") or "")]
    for event in events:
        values.append(str(event.get("created_at") or ""))
        payload = _parse_args_payload(event.get("payload_json", ""))
        if payload.get("ready_at"):
            values.append(str(payload.get("ready_at") or ""))
    return values


def _format_show_timestamp(value: str, *, pad_day: bool) -> str:
    parsed = parse_utc(value)
    if parsed is None:
        return "-"
    local = parsed.astimezone()
    time_text = color(local.strftime("%H:%M"), PALETTE.subtle)
    day = f"{local.day:2d}" if pad_day else str(local.day)
    date_text = color(f"on {local.strftime('%b')} {day}", PALETTE.dim)
    return f"{time_text} {date_text}"


def _quoted_reason(reason: str) -> str:
    if not reason:
        return ""
    return " " + color(json.dumps(reason, ensure_ascii=False), PALETTE.muted)


def _format_substate(value: str) -> str:
    text = str(value or "")
    if text == "needs_help":
        return color(text, PALETTE.error, bold=True)
    if text == "interaction":
        return color(text, PALETTE.warn, bold=True)
    return color(text, PALETTE.accent)


def _strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def _render_list_issues(conn: Any, *, daemon_active: bool) -> list[str]:
    lines: list[str] = []
    exit_info = daemon_exit_info(conn)
    last_seen = get_meta(conn, "list_last_seen_error_at")
    crash_already_shown = False

    if not daemon_active and exit_info["last_exit_kind"] == "error" and exit_info["last_exit_at"]:
        crash_already_shown = True
        when = _format_show_timestamp(exit_info["last_exit_at"], pad_day=False)
        lines.append(f"  {color('daemon exited with error', PALETTE.error, bold=True)} at {when}")
        for excerpt in _error_excerpt(exit_info["last_error"]):
            lines.append(f"    {color(excerpt, PALETTE.subtle)}")

    daemon_events = [dict(row) for row in list_daemon_events(conn, since=last_seen)]
    agent_events = [dict(row) for row in list_error_events(conn, since=last_seen)]

    if daemon_events or agent_events:
        lines.append(f"  {color('new since last list:', PALETTE.muted)}")
        for event in daemon_events:
            if crash_already_shown and event["level"] == "error" and event["created_at"] == exit_info["last_error_at"]:
                continue
            level = str(event["level"] or "warning").lower()
            level_text = color(level, PALETTE.error if level == "error" else PALETTE.warn, bold=True)
            when = _format_show_timestamp(event["created_at"], pad_day=False)
            lines.append(f"  {when} {level_text}: {event['message']}")
            for excerpt in _error_excerpt(event.get("details_text") or ""):
                lines.append(f"    {color(excerpt, PALETTE.subtle)}")
        for event in agent_events:
            when = _format_show_timestamp(event["created_at"], pad_day=False)
            agent_label = color(f"agent #{event['agent_id']}", PALETTE.accent, bold=True)
            state_label = color(str(event.get("state_name") or event.get("current_state") or ""), PALETTE.state, bold=True)
            kind = str(event["kind"] or "")
            if kind == "needs_help":
                label = color("needs_help", PALETTE.error, bold=True)
            else:
                label = color("error", PALETTE.warn, bold=True)
            lines.append(f"  {when} {agent_label} {state_label} {label}: {event['reason']}")
    return lines


def _error_excerpt(text: str, *, max_lines: int = 6) -> list[str]:
    items = [line.rstrip() for line in text.splitlines() if line.strip()]
    if not items:
        return []
    return items[-max_lines:]
