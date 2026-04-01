from __future__ import annotations

from pathlib import Path

from flow.common import parse_wait_seconds
from flow.flowfile import load_flow, parse_start_arguments, render_flow, validate_flow


def write_flow(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_validate_reports_reserved_and_unreachable_states(tmp_path: Path) -> None:
    path = write_flow(
        tmp_path / "bad.yaml",
        """
flow:
  name: demo
  version: 1

start:
  start: true
  prompt: Hello
  transitions:
    - go: done

done:
  end: true

needs_help:
  prompt: bad
  transitions:
    - go: done
""".strip(),
    )
    flow = load_flow(path)
    result = validate_flow(flow)
    assert not result.ok
    assert any("reserved name" in item for item in result.errors)


def test_load_flow_defaults_missing_version_to_one(tmp_path: Path) -> None:
    path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo

start:
  start: true
  prompt: Hello
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )

    flow = load_flow(path)

    assert flow.version == 1
    assert validate_flow(flow).ok


def test_parse_start_arguments_renders_path_placeholders(tmp_path: Path) -> None:
    path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  path: ./repos/{{repo}}
  args:
    repo:
      default: deepseek

check:
  start: true
  prompt: Inspect {{repo}}
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    flow = load_flow(path)
    state, values, cwd = parse_start_arguments(flow, None, [])
    rendered = render_flow(flow, values, cwd_override=cwd)
    assert state == "check"
    assert values["repo"] == "deepseek"
    assert rendered.path.endswith("/repos/deepseek")


def test_parse_start_arguments_defaults_path_to_current_working_directory(tmp_path: Path, monkeypatch: object) -> None:
    path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo

check:
  start: true
  prompt: Inspect repo
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    flow = load_flow(path)
    state, values, cwd = parse_start_arguments(flow, None, [])

    assert state == "check"
    assert values == {}
    assert cwd == str(workdir.resolve())


def test_validate_wait_and_render_placeholders(tmp_path: Path) -> None:
    path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  version: 1
  args:
    minutes:
      default: "10"

check:
  start: true
  wait: "{{minutes}}m"
  transitions:
    - if: retry later
      wait: 5m
      go: check
    - go: done

done:
  end: true
""".strip(),
    )
    flow = load_flow(path)

    assert validate_flow(flow).ok

    rendered = render_flow(flow, {"minutes": "12"}, cwd_override=str(tmp_path))
    assert rendered.states["check"].wait == "12m"
    assert rendered.states["check"].transitions[0].wait == "5m"
    assert parse_wait_seconds(rendered.states["check"].wait or "") == 12 * 60


def test_validate_rejects_wait_before_end_state(tmp_path: Path) -> None:
    path = write_flow(
        tmp_path / "bad.yaml",
        """
flow:
  name: demo
  version: 1

check:
  start: true
  prompt: hi
  transitions:
    - wait: 10m
      go: done

done:
  end: true
""".strip(),
    )
    flow = load_flow(path)
    result = validate_flow(flow)
    assert not result.ok
    assert any("cannot wait before entering end state" in item for item in result.errors)
