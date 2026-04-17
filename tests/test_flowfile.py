from __future__ import annotations

from pathlib import Path

from flow.common import parse_wait_seconds
from flow.flowfile import catalog_search_paths, discover_catalog, load_flow, parse_start_arguments, render_flow, validate_flow


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

needs-help:
  prompt: bad
  transitions:
    - go: done
""".strip(),
    )
    flow = load_flow(path)
    result = validate_flow(flow)
    assert not result.ok
    assert any("reserved name" in item for item in result.errors)


def test_validate_allows_start_end_state(tmp_path: Path) -> None:
    path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo

run-once:
  start: true
  end: true
  prompt: Finish the one-shot task.
""".strip(),
    )

    flow = load_flow(path)

    assert validate_flow(flow).ok


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


def test_load_flow_parses_description_and_renders_placeholders(tmp_path: Path) -> None:
    path = write_flow(
        tmp_path / "flow.yaml",
        """
flow:
  name: demo
  description: Inspect {{repo}} and report status.

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
    rendered = render_flow(flow, {"repo": "tt-metal"}, cwd_override=str(tmp_path))

    assert flow.description == "Inspect {{repo}} and report status."
    assert rendered.description == "Inspect tt-metal and report status."


def test_validate_rejects_undeclared_placeholder(tmp_path: Path) -> None:
    path = write_flow(
        tmp_path / "bad.yaml",
        """
flow:
  name: demo

start:
  start: true
  prompt: Inspect {{repo}}
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )

    flow = load_flow(path)
    result = validate_flow(flow)

    assert not result.ok
    assert any("placeholder '{{repo}}' is used but not declared in flow.args" in item for item in result.errors)


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


def test_validate_rejects_end_state_transitions(tmp_path: Path) -> None:
    path = write_flow(
        tmp_path / "bad.yaml",
        """
flow:
  name: demo
  version: 1

done:
  start: true
  end: true
  prompt: final work
  transitions:
    - go: elsewhere

elsewhere:
  end: true
""".strip(),
    )
    flow = load_flow(path)
    result = validate_flow(flow)
    assert not result.ok
    assert any("cannot define transitions" in item for item in result.errors)


def test_validate_rejects_state_without_transitions_or_end(tmp_path: Path) -> None:
    path = write_flow(
        tmp_path / "bad.yaml",
        """
flow:
  name: demo

check:
  start: true
  prompt: hi
""".strip(),
    )

    flow = load_flow(path)
    result = validate_flow(flow)

    assert not result.ok
    assert any("set 'end: true' for clarity" in item for item in result.errors)


def test_catalog_search_paths_defaults_when_flow_path_is_unset(tmp_path: Path, monkeypatch: object) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FLOW_PATH", raising=False)

    paths = catalog_search_paths()

    assert paths == (
        (home / "flows").resolve(),
        (home / ".flow" / "flows").resolve(),
        (tmp_path / "flows").resolve(),
    )


def test_discover_catalog_returns_valid_flows_and_reports_broken_duplicates(tmp_path: Path, monkeypatch: object) -> None:
    root_a = tmp_path / "flows-a"
    root_b = tmp_path / "flows-b"
    root_a.mkdir()
    root_b.mkdir()
    write_flow(
        root_a / "watch-pr.yaml",
        """
flow:
  name: watch-pr
  description: Watch CI on a PR.
  args:
    pr:
      help: Link to the PR to watch

check:
  start: true
  prompt: check
  transitions:
    - go: success

success:
  end: true
""".strip(),
    )
    write_flow(
        root_b / "broken.yaml",
        """
flow:
  name: broken

check:
  start: true
  prompt: check
  transitions:
    - go: missing
""".strip(),
    )
    write_flow(
        root_b / "watch-pr.yaml",
        """
flow:
  name: watch-pr

check:
  start: true
  prompt: duplicate
  transitions:
    - go: done

done:
  end: true
""".strip(),
    )
    monkeypatch.setenv("FLOW_PATH", f"{root_a}:{root_b}")

    catalog = discover_catalog()

    assert [item.name for item in catalog.flows] == ["watch-pr"]
    assert catalog.flows[0].args == {"pr": "Link to the PR to watch"}
    assert catalog.flows[0].end_states == ("success",)
    assert len(catalog.broken) == 2
    assert any("missing" in item.error for item in catalog.broken)
    assert any("duplicate flow name" in item.error for item in catalog.broken)
