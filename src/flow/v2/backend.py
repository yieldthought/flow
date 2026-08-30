"""Codex Python SDK backend for the foreground runtime."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai_codex import ApprovalMode, AsyncCodex, AsyncTurnHandle, CodexConfig, Sandbox
from openai_codex.generated.v2_all import (
    AgentMessageDeltaNotification,
    AgentMessageThreadItem,
    ItemCompletedNotification,
    MessagePhase,
    ReasoningEffort,
    TurnCompletedNotification,
)

from .spec import FlowSpec, StateSpec

ActivityCallback = Callable[[str], None]
TurnStartedCallback = Callable[[str], None]


@dataclass(frozen=True)
class BackendTurn:
    id: str
    status: str
    final_response: str = ""
    error: str = ""


class AgentBackend(ABC):
    @abstractmethod
    async def open(self, flow: FlowSpec, thread_id: str = "", scratchpad: str = "") -> str:
        raise NotImplementedError

    @abstractmethod
    async def run_turn(
        self,
        prompt: str,
        *,
        state: StateSpec,
        output_schema: dict[str, Any] | None = None,
        on_activity: ActivityCallback | None = None,
        on_started: TurnStartedCallback | None = None,
    ) -> BackendTurn:
        raise NotImplementedError

    @abstractmethod
    async def recover_turn(self, turn_id: str) -> BackendTurn | None:
        raise NotImplementedError

    @abstractmethod
    async def interrupt_active(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        raise NotImplementedError


class CodexBackend(AgentBackend):
    def __init__(self) -> None:
        self.codex: AsyncCodex | None = None
        self.thread: Any = None
        self.flow: FlowSpec | None = None
        self.active: AsyncTurnHandle | None = None

    async def open(self, flow: FlowSpec, thread_id: str = "", scratchpad: str = "") -> str:
        self.flow = flow
        config = CodexConfig(
            cwd=flow.path,
            client_name="flow",
            client_title="Flow 2.0",
            config_overrides=("check_for_update_on_startup=false",),
        )
        self.codex = AsyncCodex(config=config)
        await self.codex.__aenter__()
        approval, sandbox = _mode_settings(flow.mode)
        thread_config: dict[str, Any] | None = None
        if scratchpad:
            thread_config = {
                "sandbox_workspace_write": {
                    "writable_roots": [str(Path(scratchpad).expanduser().resolve().parent)]
                }
            }
        if thread_id:
            self.thread = await self.codex.thread_resume(
                thread_id,
                approval_mode=approval,
                config=thread_config,
                cwd=flow.path,
                model=flow.model,
                sandbox=sandbox,
                service_tier="fast" if flow.fast else None,
            )
        else:
            self.thread = await self.codex.thread_start(
                approval_mode=approval,
                config=thread_config,
                cwd=flow.path,
                developer_instructions=(
                    "You are executing one state at a time under Flow 2.0. Follow the current state prompt, "
                    "keep durable notes in the scratchpad named by the prompt, and do not invent or execute "
                    "workflow transitions yourself. Flow will ask for a structured transition decision."
                ),
                ephemeral=False,
                model=flow.model,
                sandbox=sandbox,
                service_name="flow",
                service_tier="fast" if flow.fast else None,
            )
            try:
                await self.thread.set_name(flow.name)
            except Exception:
                pass
        return str(self.thread.id)

    async def run_turn(
        self,
        prompt: str,
        *,
        state: StateSpec,
        output_schema: dict[str, Any] | None = None,
        on_activity: ActivityCallback | None = None,
        on_started: TurnStartedCallback | None = None,
    ) -> BackendTurn:
        if self.thread is None or self.flow is None:
            raise RuntimeError("Codex backend is not open")
        mode = state.mode or self.flow.mode
        thinking = state.thinking or self.flow.thinking
        fast = state.fast if state.fast is not None else self.flow.fast
        approval, sandbox = _mode_settings(mode)
        self.active = await self.thread.turn(
            prompt,
            approval_mode=approval,
            cwd=self.flow.path,
            effort=ReasoningEffort(thinking),
            model=self.flow.model,
            output_schema=output_schema,
            sandbox=sandbox,
            service_tier="fast" if fast else None,
        )
        turn_id = self.active.id
        if on_started is not None:
            on_started(turn_id)
        try:
            return await self._consume(self.active, on_activity=on_activity)
        finally:
            if self.active is not None and self.active.id == turn_id:
                self.active = None

    async def recover_turn(self, turn_id: str) -> BackendTurn | None:
        if self.thread is None or self.codex is None:
            raise RuntimeError("Codex backend is not open")
        response = await self.thread.read(include_turns=True)
        for turn in response.thread.turns:
            if turn.id != turn_id:
                continue
            status = _enum_value(turn.status)
            error = turn.error.message if turn.error is not None and turn.error.message else ""
            final = _final_response(turn.items)
            if status == "inProgress":
                handle = AsyncTurnHandle(self.codex, self.thread.id, turn.id)
                try:
                    await handle.interrupt()
                except Exception:
                    pass
                return BackendTurn(turn.id, "interrupted", final, "active turn interrupted during recovery")
            return BackendTurn(turn.id, status, final, error)
        return None

    async def interrupt_active(self) -> None:
        if self.active is None:
            return
        try:
            await self.active.interrupt()
        except Exception:
            pass

    async def close(self) -> None:
        codex, self.codex = self.codex, None
        self.active = None
        self.thread = None
        if codex is not None:
            await codex.close()

    @staticmethod
    async def _consume(handle: AsyncTurnHandle, on_activity: ActivityCallback | None) -> BackendTurn:
        final = ""
        status = "failed"
        error = ""
        activity = ""
        async for event in handle.stream():
            payload = event.payload
            if isinstance(payload, AgentMessageDeltaNotification):
                activity = (activity + payload.delta)[-2000:]
                if on_activity is not None and ("\n" in payload.delta or len(activity) >= 160):
                    on_activity(activity)
            elif isinstance(payload, ItemCompletedNotification):
                item = payload.item.root if hasattr(payload.item, "root") else payload.item
                if isinstance(item, AgentMessageThreadItem):
                    if item.phase == MessagePhase.final_answer or not final:
                        final = item.text
            elif isinstance(payload, TurnCompletedNotification):
                status = _enum_value(payload.turn.status)
                if payload.turn.error is not None:
                    error = payload.turn.error.message or ""
        if on_activity is not None and activity:
            on_activity(activity)
        return BackendTurn(handle.id, status, final, error)


def _mode_settings(mode: str) -> tuple[ApprovalMode, Sandbox]:
    if mode in {"yolo", "danger-full-access"}:
        return ApprovalMode.deny_all, Sandbox.full_access
    if mode == "full-auto":
        return ApprovalMode.deny_all, Sandbox.workspace_write
    return ApprovalMode.auto_review, Sandbox.workspace_write


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _final_response(items: list[Any]) -> str:
    fallback = ""
    for wrapped in reversed(items):
        item = wrapped.root if hasattr(wrapped, "root") else wrapped
        if not isinstance(item, AgentMessageThreadItem):
            continue
        if item.phase == MessagePhase.final_answer:
            return item.text
        if not fallback:
            fallback = item.text
    return fallback
