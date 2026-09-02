from __future__ import annotations

from importlib.metadata import distribution
from pathlib import Path

import flow


ROOT = Path(__file__).resolve().parents[1]


def test_public_commands_route_to_current_and_compatibility_runtimes() -> None:
    package = distribution("flow-like-a-river")
    scripts = {
        entry.name: entry.value
        for entry in package.entry_points
        if entry.group == "console_scripts" and entry.name.startswith("flow")
    }

    assert scripts == {
        "flow": "flow.v2.cli:main",
        "flow1": "flow.cli:main",
    }
    assert package.version == flow.__version__ == "2.1.1"


def test_flow_2_examples_use_the_canonical_shebang() -> None:
    examples = sorted((ROOT / "examples").glob("*.flow"))

    assert examples
    assert all(path.read_text(encoding="utf-8").startswith("#!/usr/bin/env flow\n") for path in examples)
    assert not list((ROOT / "examples").glob("*.yaml"))
    assert sorted((ROOT / "flow1" / "examples").glob("*.yaml"))
