from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from openai_codex.generated.v2_all import AgentMessageThreadItem, MessagePhase, ThreadItem

from flow.v2.backend import BackendTurn, CodexBackend


@pytest.mark.parametrize("requested_id", ["recorded-turn", "missing-turn"])
def test_recovery_uses_exact_sdk_turn_id(requested_id: str) -> None:
    def turn(turn_id: str, response: str) -> SimpleNamespace:
        return SimpleNamespace(
            id=turn_id,
            status="completed",
            error=None,
            items=[
                ThreadItem(AgentMessageThreadItem(
                    type="agentMessage",
                    id=f"message-{turn_id}",
                    text=response,
                    phase=MessagePhase.final_answer,
                ))
            ],
        )

    class Thread:
        async def read(self, *, include_turns: bool) -> SimpleNamespace:
            assert include_turns is True
            return SimpleNamespace(thread=SimpleNamespace(turns=[
                turn("older-turn", '{"choice":"needs-help","reason":"old response"}'),
                turn("recorded-turn", '{"choice":"done","reason":"complete"}'),
                turn("newer-turn", '{"choice":"keep-working","reason":"another response"}'),
            ]))

    backend = CodexBackend()
    backend.thread = Thread()
    backend.codex = object()  # type: ignore[assignment]

    result = asyncio.run(backend.recover_turn(requested_id))

    if requested_id == "missing-turn":
        assert result is None
    else:
        assert result == BackendTurn(
            "recorded-turn", "completed", '{"choice":"done","reason":"complete"}'
        )
