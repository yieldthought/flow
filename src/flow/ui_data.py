"""UI-facing runtime snapshots for the graph lab."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from .common import duration_seconds, format_utc, normalize_phase, parse_utc, utc_now
from .flowfile import FlowSpec, flow_from_dict
from .store import (
    daemon_exit_info,
    daemon_status,
    get_agent,
    get_flow_snapshot,
    list_agent_events,
    list_agents,
    list_daemon_events,
    list_error_events,
    state_active_seconds,
    total_active_seconds,
)


@dataclass(frozen=True)
class AgentStatus:
    name: str
    seconds: float


def build_overview_snapshot(conn: Any, flow_name: str) -> dict[str, Any]:
    agents = [dict(row) for row in list_agents(conn, flow_name)]
    if not agents:
        raise ValueError(f"unknown or inactive flow '{flow_name}'")

    snapshots = _snapshot_flows(conn, agents)
    topology = _merged_topology(agents, snapshots)
    state_rows: dict[str, dict[str, list[dict[str, Any]]]] = {
        state["name"]: {"waiting": [], "working": [], "paused": [], "needs_help": [], "finished": []}
        for state in topology["states"]
    }

    counts = {"waiting": 0, "working": 0, "paused": 0, "needs_help": 0}
    for agent in agents:
        snapshot_flow = snapshots[int(agent["flow_snapshot_id"])]
        row = _agent_row(conn, agent, snapshot_flow)
        status = str(row["status"])
        state_rows.setdefault(agent["current_state"], {"waiting": [], "working": [], "paused": [], "needs_help": [], "finished": []})
        state_rows[agent["current_state"]][status].append(row)
        if status in counts:
            counts[status] += 1

    for buckets in state_rows.values():
        for name in buckets:
            buckets[name].sort(key=lambda item: (int(item["id"])))

    return {
        "runtime": _runtime_summary(conn),
        "flow": {
            "name": flow_name,
            "counts": counts,
            "states": [
                {
                    **state,
                    "rows": state_rows.get(state["name"], {"waiting": [], "working": [], "paused": [], "needs_help": [], "finished": []}),
                }
                for state in topology["states"]
            ],
            "edges": topology["edges"],
        },
    }


def build_focus_snapshot(conn: Any, flow_name: str, agent_id: int) -> dict[str, Any]:
    overview = build_overview_snapshot(conn, flow_name)
    row = get_agent(conn, agent_id)
    if row is None:
        raise ValueError(f"unknown agent {agent_id}")
    agent = dict(row)
    if agent["flow_name"] != flow_name:
        raise ValueError(f"agent {agent_id} is not in flow '{flow_name}'")

    events = [dict(item) for item in list_agent_events(conn, agent_id)]
    state_summaries = _state_visit_summaries(conn, agent)
    edge_summaries = _edge_summaries(events)
    focus_states = {state["name"] for state in overview["flow"]["states"]}
    focus_states.update(state_summaries)
    focus_states.update(_event_state_names(events))
    focus_edges = {edge["key"] for edge in overview["flow"]["edges"]}
    focus_edges.update(edge_summaries)

    overview["focus"] = {
        "agent": _focused_agent(conn, agent, _snapshot_flows(conn, [agent])[int(agent["flow_snapshot_id"])]),
        "events": [_event_item(agent, event) for event in events],
        "states": {
            name: state_summaries.get(name, _empty_state_summary(name))
            for name in sorted(focus_states)
        },
        "edges": {
            key: edge_summaries[key]
            for key in sorted(focus_edges)
            if key in edge_summaries
        },
    }
    overview["flow"]["states"] = [
        {
            **state,
            "focus": overview["focus"]["states"].get(state["name"], _empty_state_summary(state["name"])),
        }
        for state in overview["flow"]["states"]
    ]
    overview["flow"]["edges"] = [
        {
            **edge,
            "focus": overview["focus"]["edges"].get(edge["key"]),
        }
        for edge in overview["flow"]["edges"]
    ]
    return overview


def _runtime_summary(conn: Any) -> dict[str, Any]:
    status = daemon_status(conn)
    exit_info = daemon_exit_info(conn)
    active = status["active"] == "1"
    started_at = status["started_at"] or ""
    uptime_seconds = duration_seconds(started_at) if active else 0.0

    diagnostics: list[dict[str, Any]] = []
    if not active and exit_info["last_exit_kind"] == "error" and exit_info["last_exit_at"]:
        diagnostics.append(
            {
                "kind": "daemon_crash",
                "level": "error",
                "created_at": exit_info["last_exit_at"],
                "message": "Daemon exited with error",
                "details": _excerpt(exit_info["last_error"]),
            }
        )

    daemon_since = started_at if active and started_at else ""
    error_since = started_at if active and started_at else ""

    for event in list(reversed([dict(row) for row in list_daemon_events(conn, since=daemon_since)]))[:3]:
        diagnostics.append(
            {
                "kind": "daemon_event",
                "level": str(event["level"]),
                "created_at": str(event["created_at"]),
                "message": str(event["message"]),
                "details": _excerpt(str(event.get("details_text") or "")),
            }
        )
    for event in list(reversed([dict(row) for row in list_error_events(conn, since=error_since)]))[:3]:
        diagnostics.append(
            {
                "kind": str(event["kind"]),
                "level": "error" if event["kind"] == "needs_help" else "warning",
                "created_at": str(event["created_at"]),
                "message": str(event["reason"]),
                "agent_id": int(event["agent_id"]),
                "state_name": str(event.get("state_name") or event.get("current_state") or ""),
            }
        )
    diagnostics.sort(key=lambda item: str(item["created_at"]), reverse=True)
    return {
        "active": active,
        "pid": int(status["pid"]) if str(status["pid"]).isdigit() else None,
        "started_at": started_at,
        "heartbeat_at": status["heartbeat_at"] or "",
        "uptime_seconds": uptime_seconds,
        "diagnostics": diagnostics[:5],
    }


def _snapshot_flows(conn: Any, agents: list[dict[str, Any]]) -> dict[int, FlowSpec]:
    flows: dict[int, FlowSpec] = {}
    for agent in agents:
        snapshot_id = int(agent["flow_snapshot_id"])
        if snapshot_id in flows:
            continue
        snapshot = get_flow_snapshot(conn, snapshot_id)
        flows[snapshot_id] = flow_from_dict(json.loads(str(snapshot["snapshot_json"])))
    return flows


def _merged_topology(agents: list[dict[str, Any]], snapshots: dict[int, FlowSpec]) -> dict[str, Any]:
    states: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}
    for flow in snapshots.values():
        for state in flow.states.values():
            entry = states.setdefault(
                state.name,
                {
                    "id": state.name,
                    "name": state.name,
                    "start": False,
                    "end": False,
                    "synthetic": False,
                },
            )
            entry["start"] = bool(entry["start"] or state.start)
            entry["end"] = bool(entry["end"] or state.end)
            for transition in state.transitions:
                key = f"{state.name}->{transition.target}"
                entry = edges.setdefault(
                    key,
                    {
                        "id": key,
                        "key": key,
                        "source": state.name,
                        "target": transition.target,
                        "transition_labels": [],
                        "transition_label_text": "",
                    },
                )
                label = _transition_label(transition.condition, transition.wait)
                if label not in entry["transition_labels"]:
                    entry["transition_labels"].append(label)
                    entry["transition_label_text"] = " / ".join(entry["transition_labels"])
    for agent in agents:
        states.setdefault(
            str(agent["current_state"]),
            {
                "id": str(agent["current_state"]),
                "name": str(agent["current_state"]),
                "start": False,
                "end": False,
                "synthetic": True,
            },
        )
    ordered_states = sorted(states.values(), key=lambda item: (not item["start"], item["end"], item["name"]))
    ordered_edges = sorted(edges.values(), key=lambda item: (item["source"], item["target"]))
    return {"states": ordered_states, "edges": ordered_edges}


def _transition_label(condition: str | None, wait: str | None) -> str:
    del condition
    if not wait:
        return ""
    return f"[{wait} wait]"


def _agent_row(conn: Any, agent: dict[str, Any], snapshot_flow: FlowSpec) -> dict[str, Any]:
    status = _agent_status(conn, agent)
    all_args = _json_object(agent.get("args_json", ""))
    nondefault_args = {
        key: value
        for key, value in all_args.items()
        if key not in snapshot_flow.args or snapshot_flow.args[key].default != str(value)
    }
    return {
        "id": int(agent["id"]),
        "status": status.name,
        "timer_seconds": status.seconds,
        "args": all_args,
        "display_args": nondefault_args,
        "state_name": str(agent["current_state"]),
        "substate": str(agent["substate"]),
        "phase": normalize_phase(agent["phase"]),
        "cwd": str(agent["cwd"]),
        "ready_at": str(agent.get("ready_at") or ""),
        "ended_at": str(agent.get("ended_at") or ""),
    }


def _focused_agent(conn: Any, agent: dict[str, Any], snapshot_flow: FlowSpec) -> dict[str, Any]:
    status = _agent_status(conn, agent)
    args = _json_object(agent.get("args_json", ""))
    return {
        "id": int(agent["id"]),
        "flow_name": str(agent["flow_name"]),
        "current_state": str(agent["current_state"]),
        "substate": str(agent["substate"]),
        "phase": normalize_phase(agent["phase"]),
        "status": status.name,
        "timer_seconds": status.seconds,
        "status_message": str(agent.get("status_message") or ""),
        "cwd": str(agent["cwd"]),
        "args": args,
        "display_args": {key: value for key, value in args.items() if key not in snapshot_flow.args or snapshot_flow.args[key].default != str(value)},
        "ready_at": str(agent.get("ready_at") or ""),
        "ended_at": str(agent.get("ended_at") or ""),
        "created_at": str(agent["created_at"]),
        "state_options": sorted(snapshot_flow.states),
    }


def _agent_status(conn: Any, agent: dict[str, Any]) -> AgentStatus:
    if agent.get("ended_at"):
        return AgentStatus("finished", total_active_seconds(conn, int(agent["id"])))
    if agent["substate"] == "needs_help":
        return AgentStatus("needs_help", state_active_seconds(conn, int(agent["id"]), str(agent["current_state"])))
    if agent["substate"] == "interaction":
        return AgentStatus("paused", state_active_seconds(conn, int(agent["id"]), str(agent["current_state"])))
    ready_at = parse_utc(agent.get("ready_at"))
    if ready_at is not None and ready_at > utc_now():
        return AgentStatus("waiting", max(0.0, (ready_at - utc_now()).total_seconds()))
    return AgentStatus("working", state_active_seconds(conn, int(agent["id"]), str(agent["current_state"])))


def _state_visit_summaries(conn: Any, agent: dict[str, Any]) -> dict[str, dict[str, Any]]:
    now = utc_now()
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in conn.execute(
        "SELECT id, state_name, started_at, ended_at FROM state_runs WHERE agent_id=? ORDER BY started_at, id",
        (int(agent["id"]),),
    ):
        started_at = str(row["started_at"])
        ended_at = str(row["ended_at"] or "")
        seconds = duration_seconds(started_at, ended_at or format_utc(now))
        buckets[str(row["state_name"])].append(
            {
                "id": int(row["id"]),
                "started_at": started_at,
                "ended_at": ended_at,
                "duration_seconds": seconds,
                "duration_text": _compact_duration(seconds),
            }
        )

    summaries: dict[str, dict[str, Any]] = {}
    for state_name, visits in buckets.items():
        total = sum(item["duration_seconds"] for item in visits)
        latest = visits[-1]
        summaries[state_name] = {
            "state_name": state_name,
            "count": len(visits),
            "latest_duration_seconds": latest["duration_seconds"],
            "latest_duration_text": latest["duration_text"],
            "total_duration_seconds": total,
            "total_duration_text": _compact_duration(total),
            "visits": visits,
        }
    return summaries


def _edge_summaries(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        if str(event.get("kind") or "") != "decision":
            continue
        from_state = str(event.get("from_state") or "")
        to_state = str(event.get("to_state") or "")
        if not from_state or not to_state:
            continue
        key = f"{from_state}->{to_state}"
        buckets[key].append(
            {
                "id": int(event["id"]),
                "created_at": str(event["created_at"]),
                "choice": str(event.get("choice") or ""),
                "reason": str(event.get("reason") or ""),
                "absolute_time_text": _absolute_time_text(str(event["created_at"])),
            }
        )
    summaries: dict[str, dict[str, Any]] = {}
    for key, items in buckets.items():
        latest = items[-1]
        summaries[key] = {
            "key": key,
            "count": len(items),
            "latest_created_at": latest["created_at"],
            "latest_time_text": latest["absolute_time_text"],
            "latest_reason": latest["reason"],
            "items": items,
        }
    return summaries


def _event_item(agent: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    absolute = _absolute_time_text(str(event["created_at"]))
    relative = _relative_duration_text(duration_seconds(str(agent["created_at"]), str(event["created_at"])))
    text, link = _event_line(event)
    return {
        "id": int(event["id"]),
        "kind": str(event["kind"]),
        "created_at": str(event["created_at"]),
        "absolute_time_text": absolute,
        "relative_time_text": relative,
        "text": text,
        "link": link,
    }


def _event_line(event: dict[str, Any]) -> tuple[str, dict[str, str] | None]:
    kind = str(event.get("kind") or "")
    state_name = str(event.get("state_name") or "")
    from_state = str(event.get("from_state") or "")
    to_state = str(event.get("to_state") or "")
    choice = str(event.get("choice") or "")
    reason = str(event.get("reason") or "")
    payload = _json_object(event.get("payload_json", ""))

    if kind == "started":
        state = state_name or "state"
        return f"{state}    started", {"type": "state", "key": state}
    if kind == "decision":
        source = from_state or state_name or "state"
        target = to_state or choice or "decision"
        suffix = f' "{reason}"' if reason else ""
        return f"{source} -> {target}{suffix}", {"type": "edge", "key": f"{source}->{target}"} if to_state else {"type": "state", "key": source}
    if kind == "delay":
        state = state_name or from_state or "state"
        wait_value = str(payload.get("wait") or "?")
        ready_at = str(payload.get("ready_at") or "")
        suffix = _absolute_time_text(ready_at) if ready_at else "-"
        return f"{state}    wait for {wait_value} until {suffix}", {"type": "state", "key": state}
    if kind == "pause":
        state = state_name or from_state or "state"
        return _state_event_line(state, "paused", reason)
    if kind == "interrupt":
        state = state_name or from_state or "state"
        return _state_event_line(state, "interrupted", reason)
    if kind == "resume":
        state = state_name or from_state or "state"
        return _state_event_line(state, "resumed", reason)
    if kind == "wake":
        state = state_name or from_state or "state"
        return _state_event_line(state, "woke", reason)
    if kind == "needs_help":
        state = state_name or from_state or "state"
        return _state_event_line(state, "needs_help", reason)
    label = kind or "event"
    suffix = f' "{reason}"' if reason else ""
    return f"{label}{suffix}", None


def _state_event_line(state: str, label: str, reason: str) -> tuple[str, dict[str, str]]:
    suffix = f' "{reason}"' if reason else ""
    return f"{state}    {label}{suffix}", {"type": "state", "key": state}


def _event_state_names(events: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for event in events:
        for key in ("state_name", "from_state", "to_state"):
            value = str(event.get(key) or "")
            if value:
                names.add(value)
    return names


def _compact_duration(value: float) -> str:
    total_minutes = max(0, int(value) // 60)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}h {minutes}m"


def _relative_duration_text(value: float) -> str:
    total_minutes = max(0, int(value) // 60)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}h {minutes:2d}m"


def _absolute_time_text(value: str) -> str:
    parsed = parse_utc(value)
    if parsed is None:
        return "-"
    local = parsed.astimezone()
    return f"{local.strftime('%H:%M')} on {local.strftime('%b')} {local.day}"


def _excerpt(text: str, *, max_lines: int = 4) -> list[str]:
    lines = [line.rstrip() for line in str(text).splitlines() if line.strip()]
    return lines[-max_lines:]


def _json_object(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text or "{}")
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _empty_state_summary(name: str) -> dict[str, Any]:
    return {
        "state_name": name,
        "count": 0,
        "latest_duration_seconds": 0.0,
        "latest_duration_text": "0h 0m",
        "total_duration_seconds": 0.0,
        "total_duration_text": "0h 0m",
        "visits": [],
    }
