from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace
from typing import Any

from openai_codex import ApprovalMode, Sandbox
from openai_codex.generated.v2_all import (
    AgentMessageDeltaNotification,
    ItemCompletedNotification,
    MessagePhase,
    ReasoningEffort,
    ThreadItem,
)

from flow.v2.activity import (
    ACTIVITY_KINDS,
    ACTIVITY_MODEL,
    ACTIVITY_SCHEMA,
    ActivityAssessment,
    ActivityRequest,
    ActivitySummarizer,
    CodexActivityAssessor,
)
from flow.v2.backend import CodexBackend


class FakeAssessor:
    def __init__(self, responses: list[ActivityAssessment]) -> None:
        self.responses = deque(responses)
        self.requests: list[ActivityRequest] = []
        self.closed = False

    async def assess(self, request: ActivityRequest) -> ActivityAssessment:
        self.requests.append(request)
        return self.responses.popleft()

    async def close(self) -> None:
        self.closed = True


async def settle(summarizer: ActivitySummarizer) -> None:
    await asyncio.sleep(0)
    task = summarizer._task
    if task is not None:
        await task


def test_activity_requires_fresh_text_and_publishes_only_meaningful_updates() -> None:
    async def scenario() -> None:
        assessor = FakeAssessor(
            [
                ActivityAssessment(True, "Case 38 is prepared and the investigation is running."),
                ActivityAssessment(False, "This must not be published."),
            ]
        )
        summarizer = ActivitySummarizer("demo", assessor, interval=0)
        emitted: list[str] = []

        await asyncio.sleep(0)
        assert assessor.requests == []

        summarizer.submit(state="work", text="Prepared case 38 and launched the investigation.", emit=emitted.append)
        await settle(summarizer)
        summarizer.submit(state="work", text="I am loading the same skill again.", emit=emitted.append)
        await settle(summarizer)

        assert emitted == ["Case 38 is prepared and the investigation is running."]
        assert assessor.requests[1].recent_summaries == (
            "work: Case 38 is prepared and the investigation is running.",
        )
        assert assessor.requests[1].recent_text == (
            "Prepared case 38 and launched the investigation.",
        )
        await summarizer.close()
        assert assessor.closed is True

    asyncio.run(scenario())


def test_activity_interval_is_a_rate_ceiling_and_batches_fresh_messages() -> None:
    async def scenario() -> None:
        now = 100.0
        sleeps: list[float] = []

        def clock() -> float:
            return now

        async def sleep(delay: float) -> None:
            nonlocal now
            sleeps.append(delay)
            now += delay

        assessor = FakeAssessor(
            [
                ActivityAssessment(True, "First useful update."),
                ActivityAssessment(True, "Second useful update."),
            ]
        )
        summarizer = ActivitySummarizer("demo", assessor, interval=60, clock=clock, sleep=sleep)
        emitted: list[str] = []

        summarizer.submit(state="work", text="first", emit=emitted.append)
        await settle(summarizer)
        summarizer.submit(state="work", text="second", emit=emitted.append)
        summarizer.submit(state="work", text="third", emit=emitted.append)
        await settle(summarizer)

        assert sleeps == [60.0]
        assert len(assessor.requests) == 2
        assert assessor.requests[1].new_text == ("second", "third")
        assert emitted == ["First useful update.", "Second useful update."]
        await summarizer.close()

    asyncio.run(scenario())


def test_activity_assessment_receives_run_and_state_context() -> None:
    async def scenario() -> None:
        assessor = FakeAssessor([ActivityAssessment(False)])
        summarizer = ActivitySummarizer(
            "issue-to-pr",
            assessor,
            flow_goal="Turn an issue into a tested pull request.",
            arguments=(("issue", "https://example.test/issues/42"),),
            interval=0,
        )

        summarizer.submit(
            state="understand",
            text="The failure is isolated to one routing path.",
            emit=lambda _summary: None,
            state_goals=("Reach 'implement' when the failure and reproduction are understood.",),
        )
        await settle(summarizer)

        request = assessor.requests[0]
        assert request.flow_goal == "Turn an issue into a tested pull request."
        assert request.arguments == (("issue", "https://example.test/issues/42"),)
        assert request.state_goals == (
            "Reach 'implement' when the failure and reproduction are understood.",
        )
        await summarizer.close()

    asyncio.run(scenario())


def test_activity_from_a_finished_turn_is_not_emitted_late() -> None:
    class BlockingAssessor:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def assess(self, _request: ActivityRequest) -> ActivityAssessment:
            self.started.set()
            await self.release.wait()
            return ActivityAssessment(True, "Stale update.")

        async def close(self) -> None:
            self.release.set()

    async def scenario() -> None:
        assessor = BlockingAssessor()
        summarizer = ActivitySummarizer("demo", assessor, interval=0)
        emitted: list[str] = []

        summarizer.submit(state="work", text="new progress", emit=emitted.append)
        await assessor.started.wait()
        summarizer.end_turn()
        assessor.release.set()
        await settle(summarizer)

        assert emitted == []
        await summarizer.close()

    asyncio.run(scenario())


def test_activity_rejects_overlong_summary_instead_of_truncating_context() -> None:
    async def scenario() -> None:
        assessor = FakeAssessor([ActivityAssessment(True, "x" * 150)])
        summarizer = ActivitySummarizer("demo", assessor, interval=0)
        emitted: list[str] = []

        summarizer.submit(state="work", text="meaningful progress", emit=emitted.append)
        await settle(summarizer)

        assert emitted == []
        await summarizer.close()

    asyncio.run(scenario())


def test_backend_uses_only_completed_commentary_as_activity_input() -> None:
    class Handle:
        id = "turn-1"

        async def stream(self):
            payloads = [
                AgentMessageDeltaNotification(delta="partial", itemId="a", threadId="t", turnId="turn-1"),
                _completed_message("a", "First complete update.", MessagePhase.commentary),
                AgentMessageDeltaNotification(delta="more partial", itemId="b", threadId="t", turnId="turn-1"),
                _completed_message("b", "Second complete update.", MessagePhase.commentary),
                _completed_message("c", "Work is complete.", MessagePhase.final_answer),
            ]
            for payload in payloads:
                yield SimpleNamespace(payload=payload)

    async def scenario() -> None:
        activity: list[str] = []
        result = await CodexBackend._consume(Handle(), on_activity=activity.append)  # type: ignore[arg-type]
        assert activity == ["First complete update.", "Second complete update."]
        assert result.final_response == "Work is complete."

    asyncio.run(scenario())


def test_codex_activity_assessor_uses_one_ephemeral_luna_thread() -> None:
    class Handle:
        async def run(self) -> Any:
            return SimpleNamespace(
                final_response=(
                    '{"meaningful_update":true,"kind":"progress",'
                    '"summary":"A concise useful update."}'
                )
            )

        async def interrupt(self) -> None:
            return None

    class Thread:
        def __init__(self) -> None:
            self.turn_calls: list[tuple[str, dict[str, Any]]] = []

        async def turn(self, prompt: str, **kwargs: Any) -> Handle:
            self.turn_calls.append((prompt, kwargs))
            return Handle()

    class Codex:
        def __init__(self) -> None:
            self.thread = Thread()
            self.start_calls: list[dict[str, Any]] = []

        async def thread_start(self, **kwargs: Any) -> Thread:
            self.start_calls.append(kwargs)
            return self.thread

    async def scenario() -> None:
        codex = Codex()
        assessor = CodexActivityAssessor(codex, cwd="/tmp/work")  # type: ignore[arg-type]
        request = ActivityRequest(
            "demo",
            "work",
            (
                "Current main still contains the 512 KiB direct-path gate; recent Galaxy CI failed.",
            ),
            (),
            ("work: prior",),
            flow_goal="Turn a GitHub issue into a tested pull request.",
            arguments=(("issue", "https://github.com/example/repo/issues/42"),),
            state_goals=("Reach 'implement' when the failure and reproduction are understood.",),
        )

        first = await assessor.assess(request)
        second = await assessor.assess(request)

        assert first == second == ActivityAssessment(True, "A concise useful update.")
        assert len(codex.start_calls) == 1
        assert codex.start_calls[0]["ephemeral"] is True
        assert codex.start_calls[0]["model"] == ACTIVITY_MODEL
        assert codex.start_calls[0]["sandbox"] == Sandbox.read_only
        assert codex.start_calls[0]["approval_mode"] == ApprovalMode.deny_all
        assert len(codex.thread.turn_calls) == 2
        prompt, options = codex.thread.turn_calls[0]
        flat_prompt = " ".join(prompt.split())
        assert "Current main still contains the 512 KiB direct-path gate" in prompt
        assert "work: prior" in prompt
        assert "Turn a GitHub issue into a tested pull request" in prompt
        assert "https://github.com/example/repo/issues/42" in prompt
        assert "the failure and reproduction are understood" in prompt
        assert "agent thread, source commentary, tools, or scratchpad" in flat_prompt
        assert "A fact is not meaningful merely because it is new" in flat_prompt
        assert "prefer silence" in flat_prompt
        assert "never code-switch" in flat_prompt
        assert options["model"] == ACTIVITY_MODEL
        assert options["effort"] == ReasoningEffort.low
        assert ACTIVITY_SCHEMA["properties"]["summary"]["maxLength"] == 100
        assert ACTIVITY_SCHEMA["properties"]["kind"]["enum"] == list(ACTIVITY_KINDS)
        await assessor.close()

    asyncio.run(scenario())


def test_activity_prompt_redacts_sensitive_argument_values() -> None:
    class Handle:
        async def run(self) -> Any:
            return SimpleNamespace(
                final_response='{"meaningful_update":false,"kind":"none","summary":""}'
            )

        async def interrupt(self) -> None:
            return None

    class Thread:
        def __init__(self) -> None:
            self.prompt = ""

        async def turn(self, prompt: str, **_kwargs: Any) -> Handle:
            self.prompt = prompt
            return Handle()

    class Codex:
        def __init__(self) -> None:
            self.thread = Thread()

        async def thread_start(self, **_kwargs: Any) -> Thread:
            return self.thread

    async def scenario() -> None:
        codex = Codex()
        assessor = CodexActivityAssessor(codex, cwd="/tmp/work")  # type: ignore[arg-type]
        request = ActivityRequest(
            "demo",
            "work",
            ("Still investigating.",),
            (),
            (),
            arguments=(("api_token", "do-not-disclose"), ("issue", "42")),
        )

        assert await assessor.assess(request) == ActivityAssessment(False, "")
        assert "api_token: <redacted>" in codex.thread.prompt
        assert "do-not-disclose" not in codex.thread.prompt
        assert "issue: 42" in codex.thread.prompt
        await assessor.close()

    asyncio.run(scenario())


def _completed_message(
    item_id: str,
    text: str,
    phase: MessagePhase,
) -> ItemCompletedNotification:
    return ItemCompletedNotification(
        completedAtMs=1,
        item=ThreadItem(
            root={
                "type": "agentMessage",
                "id": item_id,
                "text": text,
                "phase": phase.value,
            }
        ),
        threadId="t",
        turnId="turn-1",
    )
