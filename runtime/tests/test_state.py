from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from slack_codex.models import (
    InvocationPayload,
    ThreadStatus,
)
from slack_codex.models import (
    TestInvocationPayload as AgentCoreTestPayload,
)
from slack_codex.settings import Settings
from slack_codex.state import EventDeduplicator, RuntimeState, _configure_git
from slack_codex.tools.slack_tools import set_thread_status_for_context


class FakeResult:
    def __init__(self, items: list[dict[str, Any]], turn: int) -> None:
        self._items = [
            *items,
            {"role": "assistant", "content": f"answer-{turn}"},
        ]

    def to_input_list(self) -> list[dict[str, Any]]:
        return self._items


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, list[dict[str, Any]]]] = []
        self.active = 0
        self.max_active = 0

    async def run(
        self,
        agent: Any,
        items: list[dict[str, Any]],
        *,
        context: Any,
        max_turns: int,
        hooks: Any,
    ) -> FakeResult:
        self.calls.append((agent, [*items]))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        assert context.status == "working"
        context.replied = True
        context.status = "done"
        assert max_turns == 1000
        assert hooks is not None
        return FakeResult(items, len(self.calls))


class FakeSlackClient:
    def __init__(self) -> None:
        self.posts: list[tuple[Any, ...]] = []
        self.added: list[tuple[Any, ...]] = []
        self.removed: list[tuple[Any, ...]] = []

    async def post_message(self, *args: Any) -> str:
        self.posts.append(args)
        return "3.0"

    async def remove_reaction(self, *args: Any) -> None:
        self.removed.append(args)

    async def add_reaction(self, *args: Any) -> None:
        self.added.append(args)


class FakeAgent:
    def __init__(self, model_id: str = "initial") -> None:
        self.model_id = model_id
        self.clone_calls: list[str] = []

    def clone(self, *, model: str) -> FakeAgent:
        self.clone_calls.append(model)
        return FakeAgent(model)


class FakeWebFetcher:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeMcpServer:
    def __init__(self, tools: list[Any] | None = None, error: Exception | None = None) -> None:
        self.tools = tools if tools is not None else [type("Tool", (), {"name": "WebSearch"})()]
        self.error = error
        self.connected = False
        self.cleaned = False

    async def connect(self) -> None:
        self.connected = True
        if self.error is not None:
            raise self.error

    async def list_tools(self) -> list[Any]:
        return self.tools

    async def cleanup(self) -> None:
        self.cleaned = True


class FakeOpenAIClient:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def payload(event_id: str, prompt: str, *, is_parent_message: bool = False) -> InvocationPayload:
    return InvocationPayload.model_validate(
        {
            "source": "slack",
            "event_id": event_id,
            "prompt": prompt,
            "slack": {
                "team_id": "T1",
                "channel_id": "C1",
                "thread_ts": "1.0",
                "trigger_message_ts": "1.0" if is_parent_message else "2.0",
                "slack_user_id": "U1",
            },
        }
    )


def make_state(tmp_path: Path, runner: FakeRunner) -> RuntimeState:
    settings = Settings(
        aws_region="us-east-1",
        bedrock_region="us-east-1",
        model_id="openai.gpt-5.6-luna",
        slack_bot_token_secret_arn="slack",
        github_app_credentials_secret_arn="github",
        github_repository="owner/repo",
        workspace=tmp_path,
    )
    return RuntimeState(
        settings=settings,
        openai_client=object(),  # type: ignore[arg-type]
        agent=FakeAgent(),
        slack_client=FakeSlackClient(),  # type: ignore[arg-type]
        runner=runner,
    )


class IncompleteRunner(FakeRunner):
    def __init__(
        self,
        *,
        replied: bool = False,
        status: ThreadStatus | None = None,
    ) -> None:
        super().__init__()
        self._replied = replied
        self._status = status

    async def run(
        self,
        agent: Any,
        items: list[dict[str, Any]],
        *,
        context: Any,
        max_turns: int,
        hooks: Any,
    ) -> FakeResult:
        self.calls.append((agent, [*items]))
        context.replied = self._replied
        if self._status:
            await set_thread_status_for_context(context, self._status)
        return FakeResult(items, len(self.calls))


class FailingRunner(FakeRunner):
    async def run(
        self,
        agent: Any,
        items: list[dict[str, Any]],
        *,
        context: Any,
        max_turns: int,
        hooks: Any,
    ) -> FakeResult:
        raise RuntimeError("model failed")


class StubReplyRunner(FakeRunner):
    async def run(
        self,
        agent: Any,
        items: list[dict[str, Any]],
        *,
        context: Any,
        max_turns: int,
        hooks: Any,
    ) -> FakeResult:
        self.calls.append((agent, [*items]))
        await context.slack_client.post_message(
            context.slack.channel_id,
            context.slack.thread_ts,
            f"reply-{len(self.calls)}",
        )
        context.replied = True
        await set_thread_status_for_context(context, "done")
        return FakeResult(items, len(self.calls))


async def test_state_reuses_agent_and_complete_history(tmp_path: Path) -> None:
    runner = FakeRunner()
    state = make_state(tmp_path, runner)

    await state.run(payload("E1", "first"))
    await state.run(payload("E2", "second"))

    assert runner.calls[0][0] is state.agent
    assert runner.calls[1][0] is state.agent
    assert runner.calls[1][1] == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answer-1"},
        {"role": "user", "content": "second"},
    ]
    database_files = await asyncio.to_thread(
        lambda: [*tmp_path.rglob("*.db"), *tmp_path.rglob("*.sqlite*")]
    )
    assert not database_files


async def test_parent_message_selects_model_for_the_runtime(tmp_path: Path) -> None:
    runner = FakeRunner()
    state = make_state(tmp_path, runner)
    agent = FakeAgent()
    state.agent = agent  # type: ignore[assignment]

    await state.run(payload("E1", "please investigate #terra, thanks", is_parent_message=True))
    await state.run(payload("E2", "follow up"))

    assert agent.clone_calls == ["openai.gpt-5.6-terra"]
    assert state.agent.model_id == "openai.gpt-5.6-terra"
    assert [call[0].model_id for call in runner.calls] == [
        "openai.gpt-5.6-terra",
        "openai.gpt-5.6-terra",
    ]


async def test_state_serializes_concurrent_turns(tmp_path: Path) -> None:
    runner = FakeRunner()
    state = make_state(tmp_path, runner)

    await asyncio.gather(
        state.run(payload("E1", "first")),
        state.run(payload("E2", "second")),
    )

    assert runner.max_active == 1
    assert len(state.history) == 4


async def test_state_connects_caches_and_closes_web_search_resources(tmp_path: Path) -> None:
    runner = FakeRunner()
    state = make_state(tmp_path, runner)
    server = FakeMcpServer()
    fetcher = FakeWebFetcher()
    client = FakeOpenAIClient()
    state.web_search_server = server  # type: ignore[assignment]
    state.web_fetcher = fetcher  # type: ignore[assignment]
    state.openai_client = client  # type: ignore[assignment]

    await state.start()

    assert state.started is True
    assert server.connected is True
    await state.close()
    assert server.cleaned is True
    assert fetcher.closed is True
    assert client.closed is True


async def test_state_fails_fast_when_web_search_cannot_start(tmp_path: Path) -> None:
    state = make_state(tmp_path, FakeRunner())
    server = FakeMcpServer(error=RuntimeError("gateway unavailable"))
    state.web_search_server = server  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="gateway unavailable"):
        await state.start()

    assert state.started is False
    assert server.cleaned is True


def test_event_deduplicator_is_bounded() -> None:
    events = EventDeduplicator(max_size=2)
    assert events.add("E1") is True
    assert events.add("E1") is False
    assert events.add("E2") is True
    assert events.add("E3") is True
    assert events.add("E1") is True


def test_git_uses_the_installed_github_app_credential_helper(monkeypatch) -> None:
    commands: list[list[str]] = []

    def capture(command: list[str], **_kwargs: Any) -> None:
        commands.append(command)

    monkeypatch.setattr("slack_codex.state.subprocess.run", capture)

    _configure_git()

    assert commands[-1] == [
        "git",
        "config",
        "--global",
        "credential.https://github.com.helper",
        "!/app/.venv/bin/github-app-credential",
    ]


def test_state_rejects_cross_session_reuse(tmp_path: Path) -> None:
    state = make_state(tmp_path, FakeRunner())
    state.bind_session("a" * 33)
    try:
        state.bind_session("b" * 33)
    except RuntimeError as exc:
        assert "already bound" in str(exc)
    else:
        raise AssertionError("expected cross-session reuse to fail")


async def test_state_posts_red_fallback_when_agent_does_not_reply(tmp_path: Path) -> None:
    state = make_state(tmp_path, IncompleteRunner())

    await state.run(payload("E1", "first"))

    client = state.slack_client
    assert isinstance(client, FakeSlackClient)
    assert client.posts == [
        (
            "C1",
            "1.0",
            ":warning: I couldn't finish that request. Please @codex me again to retry.",
        )
    ]
    assert client.added == [
        ("C1", "1.0", "large_yellow_circle"),
        ("C1", "2.0", "large_yellow_circle"),
        ("C1", "1.0", "red_circle"),
        ("C1", "2.0", "red_circle"),
    ]


@pytest.mark.parametrize(
    ("replied", "status"),
    [
        (True, None),
        (False, "done"),
    ],
)
async def test_state_requires_both_reply_and_valid_final_status(
    tmp_path: Path,
    replied: bool,
    status: ThreadStatus | None,
) -> None:
    state = make_state(tmp_path, IncompleteRunner(replied=replied, status=status))

    await state.run(payload("E1", "first"))

    client = state.slack_client
    assert isinstance(client, FakeSlackClient)
    assert len(client.posts) == 1
    assert client.added[-2:] == [
        ("C1", "1.0", "red_circle"),
        ("C1", "2.0", "red_circle"),
    ]


async def test_state_posts_red_fallback_when_agent_crashes(tmp_path: Path) -> None:
    state = make_state(tmp_path, FailingRunner())

    with pytest.raises(RuntimeError, match="model failed"):
        await state.run(payload("E1", "first"))

    client = state.slack_client
    assert isinstance(client, FakeSlackClient)
    assert len(client.posts) == 1
    assert client.added[-1] == ("C1", "2.0", "red_circle")


async def test_agentcore_test_mode_persists_stub_thread_and_history(
    tmp_path: Path,
) -> None:
    runner = StubReplyRunner()
    state = make_state(tmp_path, runner)
    first = AgentCoreTestPayload(
        source="test",
        event_id="T1",
        prompt="first",
    )
    second = AgentCoreTestPayload(
        source="test",
        event_id="T2",
        prompt="second",
    )

    first_result = await state.run_test(first)
    second_result = await state.run_test(second)

    assert first_result["status"] == "completed"
    assert first_result["thread_status"] == "done"
    assert first_result["command_failures"] == 0
    assert first_result["tool_calls"] == {}
    assert first_result["slack"]["posts"] == [{"text": "reply-1", "ts": "2.000000"}]
    assert second_result["slack"]["posts"] == [{"text": "reply-2", "ts": "4.000000"}]
    assert [message["text"] for message in second_result["slack"]["thread"]] == [
        "first",
        "reply-1",
        "second",
        "reply-2",
    ]
    assert runner.calls[1][1] == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answer-1"},
        {"role": "user", "content": "second"},
    ]
