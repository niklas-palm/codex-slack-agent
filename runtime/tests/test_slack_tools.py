from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from agents.tool_context import ToolContext

from slack_codex.models import InvocationContext, SlackInvocation
from slack_codex.slack_client import SlackApiError
from slack_codex.tools.slack_tools import (
    MAX_FILE_BYTES,
    ask_user,
    download_file,
    react_to_message,
    read_thread,
    reply_to_thread,
    upload_file,
)


class FakeSlackClient:
    def __init__(self) -> None:
        self.messages = [
            {
                "user": "U0",
                "text": "parent",
                "ts": "1.0",
                "files": [
                    {
                        "id": "F1",
                        "name": "input.txt",
                        "mimetype": "text/plain",
                        "size": 5,
                        "url_private_download": "hidden",
                    }
                ],
            },
            {"user": "U1", "text": "request", "ts": "2.0"},
        ]
        self.posts: list[tuple[str, str, str]] = []
        self.added: list[tuple[str, str, str]] = []
        self.removed: list[tuple[str, str, str]] = []
        self.download_limit: int | None = None
        self.upload: dict[str, Any] | None = None
        self.file_info_calls: list[str] = []

    async def get_thread(
        self,
        channel: str,
        thread_ts: str,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        assert channel == "C1"
        assert thread_ts == "1.0"
        return self.messages[:limit]

    async def post_message(self, channel: str, thread_ts: str, text: str) -> str:
        self.posts.append((channel, thread_ts, text))
        return "3.0"

    async def add_reaction(self, channel: str, timestamp: str, emoji: str) -> None:
        self.added.append((channel, timestamp, emoji))

    async def remove_reaction(self, channel: str, timestamp: str, emoji: str) -> None:
        self.removed.append((channel, timestamp, emoji))

    async def file_info(self, file_id: str) -> dict[str, Any]:
        self.file_info_calls.append(file_id)
        return {
            "id": file_id,
            "name": "input.txt",
            "mimetype": "text/plain",
            "size": 5,
            "url_private_download": "https://files.example/input.txt",
        }

    async def download(self, _url: str, *, max_bytes: int) -> bytes:
        self.download_limit = max_bytes
        return b"hello"

    async def upload_file(self, **kwargs: Any) -> dict[str, Any]:
        self.upload = kwargs
        return {"file_id": "F2", "title": kwargs["title"]}


def make_context(client: Any, workspace: Path) -> InvocationContext:
    return InvocationContext(
        slack=SlackInvocation(
            team_id="T1",
            channel_id="C1",
            thread_ts="1.0",
            trigger_message_ts="2.0",
            slack_user_id="U1",
        ),
        slack_client=client,
        workspace=workspace,
    )


async def call_tool(tool: Any, context: InvocationContext, arguments: dict[str, Any]) -> Any:
    raw_arguments = json.dumps(arguments)
    tool_context = ToolContext(
        context,
        tool_name=tool.name,
        tool_call_id="call-1",
        tool_arguments=raw_arguments,
    )
    return await tool.on_invoke_tool(tool_context, raw_arguments)


async def test_read_and_reply_are_bound_to_triggering_thread(tmp_path: Path) -> None:
    client = FakeSlackClient()
    context = make_context(client, tmp_path)

    thread = await call_tool(read_thread, context, {"limit": 100})
    reply = await call_tool(reply_to_thread, context, {"text": "*Done*"})

    assert thread["messages"][0]["files"] == [
        {
            "id": "F1",
            "name": "input.txt",
            "mimetype": "text/plain",
            "size": 5,
        }
    ]
    assert "url_private_download" not in thread["messages"][0]["files"][0]
    assert reply == {"success": True, "ts": "3.0", "duplicate": False}
    assert context.replied is True
    assert client.posts == [("C1", "1.0", "*Done*")]


async def test_identical_concurrent_replies_are_posted_once(tmp_path: Path) -> None:
    client = FakeSlackClient()
    context = make_context(client, tmp_path)

    first, second = await asyncio.gather(
        call_tool(reply_to_thread, context, {"text": "Done"}),
        call_tool(reply_to_thread, context, {"text": "Done"}),
    )

    assert {first["duplicate"], second["duplicate"]} == {False, True}
    assert first["ts"] == second["ts"] == "3.0"
    assert client.posts == [("C1", "1.0", "Done")]


async def test_ask_user_posts_question_and_sets_waiting_on_both_messages(
    tmp_path: Path,
) -> None:
    client = FakeSlackClient()
    context = make_context(client, tmp_path)

    result = await call_tool(ask_user, context, {"question": "Which branch?"})

    assert result["success"] is True
    assert context.replied is True
    assert context.waiting is True
    assert context.status == "waiting"
    assert client.posts == [("C1", "1.0", "Which branch?")]
    assert client.added == [
        ("C1", "1.0", "question"),
        ("C1", "2.0", "question"),
    ]


async def test_react_to_message_rejects_messages_outside_thread(tmp_path: Path) -> None:
    client = FakeSlackClient()
    context = make_context(client, tmp_path)

    result = await call_tool(
        react_to_message,
        context,
        {"message_ts": "99.0", "emoji": "thumbsup", "remove": False},
    )

    assert "not part of the triggering thread" in result["error"]
    assert not client.added


async def test_download_requires_thread_attachment_and_caps_transfer(tmp_path: Path) -> None:
    client = FakeSlackClient()
    context = make_context(client, tmp_path)

    rejected = await call_tool(
        download_file,
        context,
        {"file_id": "F9", "save_as": None},
    )
    downloaded = await call_tool(
        download_file,
        context,
        {"file_id": "F1", "save_as": "../../renamed.txt"},
    )

    assert "not attached" in rejected["error"]
    assert client.file_info_calls == ["F1"]
    assert client.download_limit == MAX_FILE_BYTES
    assert downloaded["path"] == str(tmp_path / "renamed.txt")
    assert (tmp_path / "renamed.txt").read_bytes() == b"hello"


async def test_upload_rejects_traversal_and_uses_bound_thread(tmp_path: Path) -> None:
    client = FakeSlackClient()
    context = make_context(client, tmp_path)
    artifact = tmp_path / "result.txt"
    artifact.write_text("result", encoding="utf-8")

    rejected = await call_tool(
        upload_file,
        context,
        {"path": "../outside.txt", "title": None, "comment": None},
    )
    uploaded = await call_tool(
        upload_file,
        context,
        {"path": "result.txt", "title": "Result", "comment": "Attached"},
    )

    assert "inside" in rejected["error"]
    assert uploaded == {"file_id": "F2", "title": "Result"}
    assert client.upload == {
        "channel": "C1",
        "thread_ts": "1.0",
        "path": artifact,
        "title": "Result",
        "comment": "Attached",
    }


async def test_slack_api_errors_are_returned_and_do_not_mark_reply(
    tmp_path: Path,
) -> None:
    client = FakeSlackClient()

    async def fail(*_args: Any) -> str:
        raise SlackApiError("chat.postMessage", "not_in_channel")

    client.post_message = fail  # type: ignore[method-assign]
    context = make_context(client, tmp_path)

    result = await call_tool(reply_to_thread, context, {"text": "Hello"})

    assert "not_in_channel" in result["error"]
    assert context.replied is False
