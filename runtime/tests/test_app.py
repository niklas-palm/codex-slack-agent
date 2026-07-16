from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from slack_codex.app import invoke, set_state_for_testing
from slack_codex.state import EventDeduplicator

SESSION_ID = "slack_" + ("a" * 64)
PAYLOAD = {
    "source": "slack",
    "event_id": "Ev1",
    "prompt": "fix it",
    "slack": {
        "team_id": "T1",
        "channel_id": "C1",
        "thread_ts": "1.0",
        "trigger_message_ts": "2.0",
        "slack_user_id": "U1",
    },
}


class BlockingState:
    def __init__(self) -> None:
        self.events = EventDeduplicator()
        self.session_id: str | None = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.payloads: list[Any] = []

    def bind_session(self, session_id: str | None) -> None:
        if not session_id:
            raise ValueError("missing session")
        if self.session_id is not None and self.session_id != session_id:
            raise RuntimeError("different session")
        self.session_id = session_id

    async def run(self, payload: Any) -> None:
        self.payloads.append(payload)
        self.started.set()
        await self.release.wait()

    async def run_test(self, payload: Any) -> dict[str, Any]:
        self.payloads.append(payload)
        return {
            "status": "completed",
            "event_id": payload.event_id,
            "slack": {"posts": [{"text": "done"}]},
        }


async def _finish(state: BlockingState) -> None:
    state.release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_invoke_returns_accepted_before_background_turn_finishes() -> None:
    state = BlockingState()
    set_state_for_testing(state)  # type: ignore[arg-type]
    try:
        response = await invoke(PAYLOAD, SimpleNamespace(session_id=SESSION_ID))

        assert response == {
            "status": "accepted",
            "event_id": "Ev1",
            "session_id": SESSION_ID,
        }
        await asyncio.wait_for(state.started.wait(), timeout=1)
        assert not state.release.is_set()
        assert len(state.payloads) == 1
    finally:
        await _finish(state)
        set_state_for_testing(None)


async def test_invoke_ignores_duplicate_event_ids() -> None:
    state = BlockingState()
    set_state_for_testing(state)  # type: ignore[arg-type]
    try:
        first = await invoke(PAYLOAD, SimpleNamespace(session_id=SESSION_ID))
        duplicate = await invoke(PAYLOAD, SimpleNamespace(session_id=SESSION_ID))

        assert first["status"] == "accepted"
        assert duplicate["status"] == "duplicate"
        await asyncio.wait_for(state.started.wait(), timeout=1)
        assert len(state.payloads) == 1
    finally:
        await _finish(state)
        set_state_for_testing(None)


async def test_invoke_rejects_invalid_payload_without_starting_work() -> None:
    state = BlockingState()
    set_state_for_testing(state)  # type: ignore[arg-type]
    try:
        response = await invoke(
            {"source": "slack", "event_id": "Ev1"},
            SimpleNamespace(session_id=SESSION_ID),
        )
        assert response["status"] == "rejected"
        assert not state.started.is_set()
    finally:
        await _finish(state)
        set_state_for_testing(None)


async def test_test_invocation_waits_for_and_returns_stub_transcript() -> None:
    state = BlockingState()
    set_state_for_testing(state)  # type: ignore[arg-type]
    try:
        response = await invoke(
            {
                "source": "test",
                "event_id": "test-1",
                "prompt": "inspect",
            },
            SimpleNamespace(session_id=SESSION_ID),
        )
        assert response == {
            "status": "completed",
            "event_id": "test-1",
            "session_id": SESSION_ID,
            "slack": {"posts": [{"text": "done"}]},
        }
        assert len(state.payloads) == 1
    finally:
        await _finish(state)
        set_state_for_testing(None)
