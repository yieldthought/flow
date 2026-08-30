from __future__ import annotations

import asyncio
import io
import json
import re
import signal
from pathlib import Path
from typing import Any

from flow.v2.backend import AgentBackend, BackendTurn
from flow.v2.constants import EX_NEEDS_HELP, EX_SIGINT
from flow.v2.output import Reporter
from flow.v2.runtime import FlowRuntime
from flow.v2.scratchpad import create_scratchpad, new_metadata, read_scratchpad
from flow.v2.spec import load_flow, render_flow


class ScriptedBackend(AgentBackend):
    def __init__(self, decisions: list[tuple[str, str]]) -> None:
        self.decisions = list(decisions)
        self.prompts: list[str] = []
        self.turn = 0
        self.thread_id = "thread-v2"

    async def open(self, flow: Any, thread_id: str = "", scratchpad: str = "") -> str:
        return thread_id or self.thread_id

    async def run_turn(
        self,
        prompt: str,
        *,
        state: Any,
        output_schema: dict[str, Any] | None = None,
        on_activity: Any = None,
        on_started: Any = None,
    ) -> BackendTurn:
        self.turn += 1
        turn_id = f"turn-{self.turn}"
        self.prompts.append(prompt)
        if on_started:
            on_started(turn_id)
        if on_activity:
            on_activity("Made visible progress on the requested state.")
        if output_schema is None:
            return BackendTurn(turn_id, "completed", "work complete")
        request_id = re.search(r"Set request_id to exactly: ([^\n]+)", prompt).group(1)  # type: ignore[union-attr]
        choice, reason = self.decisions.pop(0)
        return BackendTurn(
            turn_id,
            "completed",
            json.dumps({"request_id": request_id, "choice": choice, "reason": reason}),
        )

    async def recover_turn(self, turn_id: str) -> BackendTurn | None:
        return None

    async def interrupt_active(self) -> None:
        return None

    async def close(self) -> None:
        return None


class BlockingBackend(ScriptedBackend):
    def __init__(self) -> None:
        super().__init__([])
        self.started = asyncio.Event()
        self.interrupted = asyncio.Event()

    async def run_turn(self, prompt: str, **kwargs: Any) -> BackendTurn:
        self.turn += 1
        turn_id = f"turn-{self.turn}"
        self.prompts.append(prompt)
        kwargs["on_started"](turn_id)
        self.started.set()
        await self.interrupted.wait()
        return BackendTurn(turn_id, "interrupted")

    async def interrupt_active(self) -> None:
        self.interrupted.set()


def write_flow(tmp_path: Path, terminal_exit: int = 0) -> Path:
    path = tmp_path / "demo.flow"
    path.write_text(
        f"""
flow:
  name: demo
  version: 2
  path: .

work:
  start: true
  prompt: Do the work.
  transitions:
    - go: done

done:
  exit: {terminal_exit}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return path


def runtime_setup(tmp_path: Path, backend: AgentBackend, *, json_output: bool = True) -> tuple[FlowRuntime, Path, io.StringIO, io.StringIO]:
    flow = load_flow(write_flow(tmp_path, terminal_exit=7))
    rendered = render_flow(flow, {}, str(tmp_path))
    path = tmp_path / "flow-demo-1.md"
    metadata = new_metadata(
        flow_path=flow.source_path,
        flow_digest=flow.digest,
        flow_name=flow.name,
        argv=[str(flow.source_path)],
        arguments={},
        invocation_cwd=str(tmp_path),
        cwd=str(tmp_path),
        state=flow.start_state,
        json_output=json_output,
    )
    create_scratchpad(path, metadata)
    output, errors = io.StringIO(), io.StringIO()
    reporter = Reporter(json_output=json_output, stream=output, error_stream=errors, activity_interval=0)
    return FlowRuntime(rendered, path, metadata, backend=backend, reporter=reporter), path, output, errors


def test_runtime_returns_authored_terminal_exit_and_stable_final_event(tmp_path: Path) -> None:
    backend = ScriptedBackend([("done", "all work is complete")])
    runtime, path, output, _ = runtime_setup(tmp_path, backend)

    result = asyncio.run(runtime.run())
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    final = events[-1]
    metadata, _ = read_scratchpad(path)

    assert result == 7
    assert final["event"] == "final"
    assert final["exit_code"] == 7
    assert final["state"] == "done"
    assert final["scratchpad"] == str(path)
    assert final["thread"] == "thread-v2"
    assert final["resumable"] is False
    assert metadata["status"] == "completed"
    assert len(backend.prompts) == 2
    assert len([event for event in events if event["event"] == "activity"]) == 1


def test_needs_help_is_resumable_and_prints_both_resume_commands(tmp_path: Path) -> None:
    backend = ScriptedBackend([("needs-help", "credentials are required")])
    runtime, path, output, errors = runtime_setup(tmp_path, backend)

    result = asyncio.run(runtime.run())
    final = json.loads(output.getvalue().splitlines()[-1])
    metadata, _ = read_scratchpad(path)

    assert result == EX_NEEDS_HELP
    assert final["resumable"] is True
    assert metadata["phase"] == "evaluate"
    assert metadata["thread"] == "thread-v2"
    assert "codex resume thread-v2" in errors.getvalue()
    assert f"flow2 resume {path}" in errors.getvalue()


def test_sigint_interrupts_active_turn_without_starting_another_prompt(tmp_path: Path) -> None:
    async def scenario() -> tuple[int, FlowRuntime, BlockingBackend, Path]:
        backend = BlockingBackend()
        runtime, path, _, _ = runtime_setup(tmp_path, backend)
        task = asyncio.create_task(runtime.run())
        await backend.started.wait()
        runtime.request_stop(signal.SIGINT)
        return await task, runtime, backend, path

    result, runtime, backend, path = asyncio.run(scenario())
    metadata, _ = read_scratchpad(path)

    assert result == EX_SIGINT
    assert len(backend.prompts) == 1
    assert metadata["phase"] == "work_interrupted"
    assert metadata["resumable"] is True
    assert runtime.stop_signal == signal.SIGINT
