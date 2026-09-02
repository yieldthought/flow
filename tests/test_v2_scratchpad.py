from __future__ import annotations

from pathlib import Path

import pytest

from flow.v2.scratchpad import (
    ScratchpadLock,
    ScratchpadLockedError,
    create_scratchpad,
    default_scratchpad,
    new_metadata,
    read_scratchpad,
    repair_scratchpad,
)
from flow.v2.watch import EventJournal


def metadata(tmp_path: Path) -> dict[str, object]:
    return new_metadata(
        flow_path=str(tmp_path / "demo.flow"),
        flow_digest="abc",
        flow_name="demo",
        argv=["demo.flow"],
        arguments={},
        invocation_cwd=str(tmp_path),
        cwd=str(tmp_path),
        state="start",
        json_output=False,
    )


def test_scratchpad_is_only_checkpoint_and_repairs_removed_header(tmp_path: Path) -> None:
    path = tmp_path / "flow-demo-1.md"
    state = metadata(tmp_path)
    create_scratchpad(path, state)
    path.write_text("# Human notes\n\nkeep this\n", encoding="utf-8")

    state["phase"] = "evaluate"
    repair_scratchpad(path, state)
    restored, body = read_scratchpad(path)

    assert restored["phase"] == "evaluate"
    assert "keep this" in body
    assert not list(tmp_path.glob("*.json"))


def test_scratchpad_lock_refuses_a_second_live_runner(tmp_path: Path) -> None:
    path = tmp_path / "flow-demo-1.md"
    create_scratchpad(path, metadata(tmp_path))
    with ScratchpadLock(path):
        with pytest.raises(ScratchpadLockedError):
            with ScratchpadLock(path):
                pass
    assert not Path(f"{path}.lock").exists()


def test_scratchpad_lock_is_an_ephemeral_event_journal(tmp_path: Path) -> None:
    path = tmp_path / "flow-demo-1.md"
    create_scratchpad(path, metadata(tmp_path))
    with ScratchpadLock(path) as lock:
        lock.append_event({"event": "state", "elapsed_seconds": 1.0, "state": "start"})
        with EventJournal(path) as journal:
            assert journal.poll() == [{"event": "state", "elapsed_seconds": 1.0, "state": "start"}]
            lock.append_event({"event": "final", "elapsed_seconds": 2.0, "state": "done", "exit_code": 0})
            assert journal.poll() == [{"event": "final", "elapsed_seconds": 2.0, "state": "done", "exit_code": 0}]
            assert journal.final_event is not None
            assert journal.live is True
    assert not Path(f"{path}.lock").exists()


def test_event_journal_detects_a_replacement_lock_file(tmp_path: Path) -> None:
    path = tmp_path / "flow-demo-1.md"
    create_scratchpad(path, metadata(tmp_path))
    journal = EventJournal(path)
    try:
        with ScratchpadLock(path) as lock:
            lock.append_event({"event": "state", "state": "start"})
            journal.poll()
            assert journal.live_journal_closed is False
        with ScratchpadLock(path):
            assert journal.live_journal_closed is True
    finally:
        journal.close()


def test_default_scratchpad_uses_one_based_disambiguation(tmp_path: Path) -> None:
    first = default_scratchpad("My Review.flow", tmp_path)
    first.touch()
    second = default_scratchpad("My Review.flow", tmp_path)
    assert first.name == "flow-my-review-1.md"
    assert second.name == "flow-my-review-2.md"
