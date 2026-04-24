from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def load_flow_stress() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "tools" / "flow_stress.py"
    spec = importlib.util.spec_from_file_location("flow_stress", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def scenario(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "root_finished": True,
        "root_end_state": "success",
        "stalled": False,
        "problematic_prompt_snapshot_count": 0,
    }
    payload.update(overrides)
    return payload


def test_stress_suite_succeeds_for_clean_successes() -> None:
    flow_stress = load_flow_stress()

    assert flow_stress.suite_succeeded({"scenarios": [scenario(), scenario()]})


def test_stress_suite_fails_for_any_bad_scenario() -> None:
    flow_stress = load_flow_stress()

    bad_cases = [
        scenario(root_finished=False),
        scenario(root_end_state="failed"),
        scenario(stalled=True),
        scenario(problematic_prompt_snapshot_count=1),
    ]

    for bad in bad_cases:
        assert not flow_stress.suite_succeeded({"scenarios": [scenario(), bad]})
