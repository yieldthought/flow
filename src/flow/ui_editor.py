"""Flow-file editor helpers for the local UI server."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import yaml

from .flowfile import catalog_search_paths, flow_from_mapping, validate_flow


FLOW_SUFFIXES = {".yaml", ".yml"}


class LiteralString(str):
    pass


class FlowYamlDumper(yaml.SafeDumper):
    pass


def _literal_string_representer(dumper: yaml.Dumper, value: LiteralString) -> yaml.Node:
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")


FlowYamlDumper.add_representer(LiteralString, _literal_string_representer)


def editor_roots() -> list[str]:
    repo_root = Path(__file__).resolve().parents[2]
    candidates = [
        *catalog_search_paths(),
        repo_root / "examples",
        repo_root / "stress",
    ]
    roots: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            path = candidate.expanduser().resolve()
        except OSError:
            continue
        if path in seen:
            continue
        seen.add(path)
        if path.exists():
            roots.append(path)
    return [str(path) for path in roots]


def list_editor_files() -> dict[str, Any]:
    roots = [Path(item) for item in editor_roots()]
    seen: set[Path] = set()
    files: list[dict[str, Any]] = []
    for root in roots:
        for candidate in _flow_candidates(root):
            if candidate in seen:
                continue
            seen.add(candidate)
            files.append(_summarize_file(candidate))
    files.sort(key=lambda item: (str(item["root"]), str(item["name"]), str(item["path"])))
    return {"roots": [str(root) for root in roots], "files": files}


def load_editor_document(path_text: str) -> dict[str, Any]:
    path = resolve_editor_path(path_text)
    yaml_text = path.read_text(encoding="utf-8")
    data = _load_yaml_mapping(yaml_text)
    validation = _validate_mapping(data, source_path=str(path))

    flow_block = data.get("flow") if isinstance(data.get("flow"), dict) else {}
    assert isinstance(flow_block, dict)
    states: list[dict[str, Any]] = []
    for index, (name, raw) in enumerate(data.items()):
        if name == "flow" or not isinstance(raw, dict):
            continue
        states.append(_state_to_editor(str(name), raw, index))

    return {
        "path": str(path),
        "fileName": path.name,
        "yaml": yaml_text,
        "flow": _flow_to_editor(path, flow_block),
        "states": states,
        "validation": validation,
    }


def validate_editor_document(document: dict[str, Any]) -> dict[str, Any]:
    data = editor_document_to_mapping(document)
    preflight_errors = _document_preflight_errors(document)
    validation = _validate_mapping(data, source_path=str(document.get("path") or ""))
    if preflight_errors:
        validation = {
            **validation,
            "ok": False,
            "errors": [*preflight_errors, *validation["errors"]],
        }
    return validation


def save_editor_document(document: dict[str, Any]) -> dict[str, Any]:
    path = resolve_editor_path(str(document.get("path") or ""))
    validation = validate_editor_document({**document, "path": str(path)})
    if validation["errors"]:
        raise ValueError("; ".join(validation["errors"]))
    data = editor_document_to_mapping(document)
    rendered = yaml.dump(
        data,
        Dumper=FlowYamlDumper,
        sort_keys=False,
        allow_unicode=True,
        width=100,
    )
    path.write_text(rendered, encoding="utf-8")
    return load_editor_document(str(path))


def editor_document_to_mapping(document: dict[str, Any]) -> dict[str, Any]:
    flow = dict(document.get("flow") or {})
    flow_block: dict[str, Any] = {}
    flow_block["name"] = str(flow.get("name") or "").strip()
    _put_optional_string(flow_block, "description", flow.get("description"))
    version = str(flow.get("version") or "").strip()
    if version:
        try:
            flow_block["version"] = int(version)
        except ValueError:
            flow_block["version"] = version
    _put_optional_string(flow_block, "path", flow.get("path"))
    _put_optional_string(flow_block, "mode", flow.get("mode"))
    _put_optional_string(flow_block, "thinking", flow.get("thinking"))
    if isinstance(flow.get("fast"), bool):
        flow_block["fast"] = bool(flow["fast"])
    args = _args_to_mapping(list(flow.get("args") or []))
    if args:
        flow_block["args"] = args

    data: dict[str, Any] = {"flow": flow_block}
    for state in list(document.get("states") or []):
        if not isinstance(state, dict):
            continue
        name = str(state.get("name") or "").strip()
        if not name:
            name = "unnamed-state"
        data[name] = _editor_state_to_mapping(state)
    return data


def resolve_editor_path(path_text: str) -> Path:
    if not path_text.strip():
        raise ValueError("flow path is required")
    raw = Path(path_text).expanduser()
    if raw.is_absolute():
        path = raw.resolve()
    else:
        path = _resolve_relative_editor_path(raw)
    if path.suffix.lower() not in FLOW_SUFFIXES:
        raise ValueError("flow editor only opens .yaml and .yml files")
    roots = [Path(item).resolve() for item in editor_roots()]
    if not roots or not any(_is_relative_to(path, root) for root in roots):
        raise ValueError("flow file is outside configured Flow roots")
    return path


def _resolve_relative_editor_path(raw: Path) -> Path:
    for root_text in editor_roots():
        candidate = (Path(root_text) / raw).resolve()
        if candidate.exists():
            return candidate
    return (Path.cwd() / raw).resolve()


def _flow_candidates(root: Path) -> list[Path]:
    if root.is_file() and root.suffix.lower() in FLOW_SUFFIXES:
        return [root.resolve()]
    if not root.is_dir():
        return []
    candidates: list[Path] = []
    for suffix in ("*.yaml", "*.yml"):
        candidates.extend(path.resolve() for path in root.rglob(suffix) if ".git" not in path.parts)
    return sorted(candidates)


def _summarize_file(path: Path) -> dict[str, Any]:
    try:
        data = _load_yaml_mapping(path.read_text(encoding="utf-8"))
        validation = _validate_mapping(data, source_path=str(path))
        flow_block = data.get("flow") if isinstance(data.get("flow"), dict) else {}
        states = [raw for key, raw in data.items() if key != "flow" and isinstance(raw, dict)]
        transition_count = sum(len(raw.get("transitions") or []) for raw in states)
        name = str(flow_block.get("name") or path.stem) if isinstance(flow_block, dict) else path.stem
        description = str(flow_block.get("description") or "") if isinstance(flow_block, dict) else ""
    except Exception as exc:
        validation = {"ok": False, "errors": [str(exc)], "warnings": []}
        name = path.stem
        description = ""
        states = []
        transition_count = 0
    return {
        "path": str(path),
        "root": _root_label(path),
        "name": name,
        "description": description,
        "valid": bool(validation["ok"]),
        "errors": validation["errors"],
        "warnings": validation["warnings"],
        "stateCount": len(states),
        "transitionCount": transition_count,
        "updatedAt": _mtime_text(path),
    }


def _load_yaml_mapping(yaml_text: str) -> dict[str, Any]:
    data = yaml.safe_load(yaml_text) or {}
    if not isinstance(data, dict):
        raise ValueError("flow file must contain a mapping at the top level")
    return data


def _validate_mapping(data: dict[str, Any], *, source_path: str) -> dict[str, Any]:
    try:
        flow = flow_from_mapping(data, source_path=source_path)
        result = validate_flow(flow)
        return {
            "ok": result.ok,
            "errors": list(result.errors),
            "warnings": list(result.warnings),
        }
    except Exception as exc:
        return {"ok": False, "errors": [str(exc)], "warnings": []}


def _flow_to_editor(path: Path, flow_block: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": str(flow_block.get("name") or path.stem),
        "description": str(flow_block.get("description") or ""),
        "version": "" if "version" not in flow_block else str(flow_block.get("version")),
        "path": str(flow_block.get("path") or ""),
        "mode": str(flow_block.get("mode") or ""),
        "thinking": str(flow_block.get("thinking") or ""),
        "fast": flow_block.get("fast") if isinstance(flow_block.get("fast"), bool) else None,
        "args": _args_to_editor(flow_block.get("args") or {}),
    }


def _args_to_editor(raw_args: Any) -> list[dict[str, str]]:
    if not isinstance(raw_args, dict):
        return []
    args: list[dict[str, str]] = []
    for name, spec in raw_args.items():
        if isinstance(spec, dict):
            help_text = spec.get("help") or ""
            default = spec.get("default")
        else:
            help_text = ""
            default = spec
        args.append(
            {
                "name": str(name),
                "help": str(help_text),
                "default": "" if default is None else str(default),
            }
        )
    return args


def _args_to_mapping(args: list[Any]) -> dict[str, dict[str, str]]:
    mapped: dict[str, dict[str, str]] = {}
    for item in args:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        spec: dict[str, str] = {}
        _put_optional_string(spec, "help", item.get("help"))
        _put_optional_string(spec, "default", item.get("default"))
        mapped[name] = spec
    return mapped


def _state_to_editor(name: str, raw: dict[str, Any], index: int) -> dict[str, Any]:
    return {
        "id": f"state-{index}-{name}",
        "name": name,
        "start": bool(raw.get("start")),
        "end": bool(raw.get("end")),
        "prompt": str(raw.get("prompt") or ""),
        "wait": str(raw.get("wait") or ""),
        "mode": str(raw.get("mode") or ""),
        "thinking": str(raw.get("thinking") or ""),
        "fast": raw.get("fast") if isinstance(raw.get("fast"), bool) else None,
        "transitions": [
            _transition_to_editor(name, item, transition_index)
            for transition_index, item in enumerate(raw.get("transitions") or [])
            if isinstance(item, dict)
        ],
    }


def _transition_to_editor(state_name: str, raw: dict[str, Any], index: int) -> dict[str, str]:
    return {
        "id": f"transition-{state_name}-{index}",
        "condition": str(raw.get("if") or ""),
        "wait": str(raw.get("wait") or ""),
        "target": str(raw.get("go") or ""),
    }


def _editor_state_to_mapping(state: dict[str, Any]) -> dict[str, Any]:
    mapped: dict[str, Any] = {}
    if bool(state.get("start")):
        mapped["start"] = True
    if bool(state.get("end")):
        mapped["end"] = True
    _put_optional_string(mapped, "wait", state.get("wait"))
    _put_optional_literal(mapped, "prompt", state.get("prompt"))
    _put_optional_string(mapped, "mode", state.get("mode"))
    _put_optional_string(mapped, "thinking", state.get("thinking"))
    if isinstance(state.get("fast"), bool):
        mapped["fast"] = bool(state["fast"])
    if not bool(state.get("end")):
        transitions = [
            _editor_transition_to_mapping(item)
            for item in list(state.get("transitions") or [])
            if isinstance(item, dict)
        ]
        if transitions:
            mapped["transitions"] = transitions
    return mapped


def _editor_transition_to_mapping(transition: dict[str, Any]) -> dict[str, Any]:
    mapped: dict[str, Any] = {}
    _put_optional_string(mapped, "if", transition.get("condition"))
    _put_optional_string(mapped, "wait", transition.get("wait"))
    mapped["go"] = str(transition.get("target") or "").strip()
    return mapped


def _put_optional_string(target: dict[str, Any], key: str, value: Any) -> None:
    text = str(value or "").strip()
    if text:
        target[key] = text


def _put_optional_literal(target: dict[str, Any], key: str, value: Any) -> None:
    text = str(value or "")
    if not text:
        return
    target[key] = LiteralString(text) if "\n" in text else text


def _document_preflight_errors(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    flow = dict(document.get("flow") or {})
    if not str(flow.get("name") or "").strip():
        errors.append("flow.name is required")
    states = [item for item in list(document.get("states") or []) if isinstance(item, dict)]
    names = [str(state.get("name") or "").strip() for state in states]
    if any(not name for name in names):
        errors.append("state names are required")
    duplicates = sorted({name for name in names if name and names.count(name) > 1})
    for name in duplicates:
        errors.append(f"state '{name}' is defined more than once")
    for state in states:
        state_name = str(state.get("name") or "").strip() or "unnamed-state"
        if bool(state.get("end")) and list(state.get("transitions") or []):
            errors.append(f"end state '{state_name}' cannot define transitions")
    return errors


def _root_label(path: Path) -> str:
    for root_text in editor_roots():
        root = Path(root_text)
        if _is_relative_to(path, root):
            return str(root)
    return str(path.parent)


def _mtime_text(path: Path) -> str:
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(path.stat().st_mtime))
    except OSError:
        return ""


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
