from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import slack_codex.app as app_module
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


class LifecycleState:
    def __init__(self, startup_error: Exception | None = None) -> None:
        self.startup_error = startup_error
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True
        if self.startup_error is not None:
            raise self.startup_error

    async def close(self) -> None:
        self.closed = True


async def _finish(state: BlockingState) -> None:
    state.release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_lifespan_starts_and_closes_runtime_state(monkeypatch) -> None:
    state = LifecycleState()
    monkeypatch.setattr(
        app_module.Settings,
        "from_env",
        classmethod(lambda _cls: object()),
    )
    monkeypatch.setattr(
        app_module.RuntimeState,
        "create",
        classmethod(lambda _cls, _settings: state),
    )

    async with app_module.lifespan(app_module.app):
        assert state.started is True
        assert app_module.get_state() is state

    assert state.closed is True
    with pytest.raises(RuntimeError, match="not been initialized"):
        app_module.get_state()


async def test_lifespan_fails_fast_and_releases_partial_state(monkeypatch) -> None:
    state = LifecycleState(startup_error=RuntimeError("gateway unavailable"))
    monkeypatch.setattr(
        app_module.Settings,
        "from_env",
        classmethod(lambda _cls: object()),
    )
    monkeypatch.setattr(
        app_module.RuntimeState,
        "create",
        classmethod(lambda _cls, _settings: state),
    )

    with pytest.raises(RuntimeError, match="gateway unavailable"):
        async with app_module.lifespan(app_module.app):
            raise AssertionError("unreachable")

    assert state.closed is True


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
