from __future__ import annotations

from pathlib import Path
from typing import Any

from slack_codex.models import InvocationContext, SlackInvocation
from slack_codex.tools.slack_tools import STATUS_EMOJI, set_thread_status_for_context


class FakeSlackClient:
    def __init__(self) -> None:
        self.removed: list[tuple[str, str, str]] = []
        self.added: list[tuple[str, str, str]] = []

    async def remove_reaction(self, channel: str, timestamp: str, emoji: str) -> None:
        self.removed.append((channel, timestamp, emoji))

    async def add_reaction(self, channel: str, timestamp: str, emoji: str) -> None:
        self.added.append((channel, timestamp, emoji))


def make_context(client: Any, *, trigger_ts: str = "2.0") -> InvocationContext:
    return InvocationContext(
        slack=SlackInvocation(
            team_id="T1",
            channel_id="C1",
            thread_ts="1.0",
            trigger_message_ts=trigger_ts,
            slack_user_id="U1",
        ),
        slack_client=client,
        workspace=Path("/workspace"),
    )


async def test_status_replaces_only_status_reactions_and_keeps_eyes() -> None:
    client = FakeSlackClient()
    result = await set_thread_status_for_context(make_context(client), "working")

    assert result["success"] is True
    assert client.added == [
        ("C1", "1.0", "large_yellow_circle"),
        ("C1", "2.0", "large_yellow_circle"),
    ]
    assert len(client.removed) == 2 * len(STATUS_EMOJI)
    assert all(emoji != "eyes" for _, _, emoji in client.removed)


async def test_status_deduplicates_parent_and_trigger() -> None:
    client = FakeSlackClient()
    await set_thread_status_for_context(make_context(client, trigger_ts="1.0"), "done")
    assert client.added == [("C1", "1.0", "large_green_circle")]
