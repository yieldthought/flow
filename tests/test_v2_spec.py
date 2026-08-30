from __future__ import annotations

from pathlib import Path

import pytest

from flow.v2.spec import load_flow, parse_arguments, render_flow, validate_flow


def write_flow(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def test_v2_parses_shebang_arguments_and_terminal_exit(tmp_path: Path) -> None:
    path = write_flow(
        tmp_path / "review.flow",
        """
#!/usr/bin/env flow
flow:
  name: review-{{repo}}
  version: 2
  path: work/{{repo}}
  args:
    repo:
      help: Repository name

review:
  start: true
  prompt: Review {{repo}}.
  transitions:
    - go: complete

complete:
  exit: 7
""",
    )

    flow = load_flow(path)
    result = validate_flow(flow)
    values, cwd = parse_arguments(flow, ["--repo", "metal"], str(tmp_path))
    rendered = render_flow(flow, values, cwd)

    assert result.ok
    assert rendered.name == "review-metal"
    assert rendered.states["complete"].exit_code == 7
    assert rendered.path == str((tmp_path / "work" / "metal").resolve())


def test_v2_rejects_v1_end_and_reserved_exit_codes(tmp_path: Path) -> None:
    v1 = write_flow(
        tmp_path / "old.flow",
        """
flow:
  name: old
  version: 2
done:
  start: true
  end: true
""",
    )
    with pytest.raises(ValueError, match="use 'exit: N'"):
        load_flow(v1)

    invalid = write_flow(
        tmp_path / "invalid.flow",
        """
flow:
  name: invalid
  version: 2
done:
  start: true
  exit: 75
""",
    )
    result = validate_flow(load_flow(invalid))
    assert not result.ok
    assert any("between 0 and 63" in error for error in result.errors)


def test_v2_requires_exactly_one_start_state(tmp_path: Path) -> None:
    path = write_flow(
        tmp_path / "two.flow",
        """
flow:
  name: two
  version: 2
one:
  start: true
  exit: 0
two:
  start: true
  exit: 0
""",
    )
    result = validate_flow(load_flow(path))
    assert any("exactly one start state" in error for error in result.errors)


def test_v2_rejects_reserved_state_names_and_nonterminating_graphs(tmp_path: Path) -> None:
    reserved = write_flow(
        tmp_path / "reserved.flow",
        """
flow:
  name: reserved
  version: 2
needs-help:
  start: true
  exit: 0
""",
    )
    assert any("reserved implicit action" in error for error in validate_flow(load_flow(reserved)).errors)

    endless = write_flow(
        tmp_path / "endless.flow",
        """
flow:
  name: endless
  version: 2
again:
  start: true
  prompt: Continue.
  transitions:
    - go: again
""",
    )
    assert any("reachable terminal" in error for error in validate_flow(load_flow(endless)).errors)
