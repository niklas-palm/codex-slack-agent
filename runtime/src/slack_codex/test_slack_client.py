from __future__ import annotations

import asyncio
import base64
import binascii
from pathlib import Path
from typing import Any

from slack_codex.models import SlackInvocation, TestAttachment
from slack_codex.slack_client import SlackApiError, SlackClient

MAX_TEST_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_RETURNED_UPLOAD_BYTES = 5 * 1024 * 1024


class StubSlackClient(SlackClient):
    """In-memory Slack implementation for synchronous AgentCore test invokes."""

    def __init__(self) -> None:
        self._messages: list[dict[str, Any]] = []
        self._posts: list[dict[str, Any]] = []
        self._reaction_events: list[dict[str, str]] = []
        self._reactions: dict[tuple[str, str], set[str]] = {}
        self._files: dict[str, dict[str, Any]] = {}
        self._uploads: list[dict[str, Any]] = []
        self._thread_ts: str | None = None
        self._sequence = 0

    def _next_timestamp(self) -> str:
        self._sequence += 1
        return f"{self._sequence}.000000"

    def _next_file_id(self) -> str:
        return f"FTEST{len(self._files) + 1:04d}"

    def start_turn(
        self,
        prompt: str,
        user_id: str,
        attachments: list[TestAttachment],
    ) -> SlackInvocation:
        timestamp = self._next_timestamp()
        if self._thread_ts is None:
            self._thread_ts = timestamp

        files = [self._add_attachment(item) for item in attachments]
        message: dict[str, Any] = {
            "user": user_id,
            "text": prompt,
            "ts": timestamp,
        }
        if files:
            message["files"] = files
        self._messages.append(message)
        self._reactions[("CLOCAL", timestamp)] = {"eyes"}
        self._reaction_events.append(
            {"action": "add", "message_ts": timestamp, "emoji": "eyes"}
        )

        return SlackInvocation(
            team_id="TLOCAL",
            channel_id="CLOCAL",
            thread_ts=self._thread_ts,
            trigger_message_ts=timestamp,
            slack_user_id=user_id,
        )

    def _add_attachment(self, attachment: TestAttachment) -> dict[str, Any]:
        try:
            content = base64.b64decode(attachment.content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"invalid base64 attachment: {attachment.name}") from exc
        if len(content) > MAX_TEST_ATTACHMENT_BYTES:
            raise ValueError(
                f"test attachment exceeds {MAX_TEST_ATTACHMENT_BYTES} bytes: {attachment.name}"
            )
        file_id = self._next_file_id()
        info = {
            "id": file_id,
            "name": Path(attachment.name).name,
            "mimetype": attachment.mimetype,
            "size": len(content),
            "url_private_download": f"stub://{file_id}",
        }
        self._files[file_id] = {**info, "content": content}
        return {key: info[key] for key in ("id", "name", "mimetype", "size")}

    def checkpoint(self) -> tuple[int, int, int]:
        return len(self._posts), len(self._reaction_events), len(self._uploads)

    def snapshot(self, checkpoint: tuple[int, int, int]) -> dict[str, Any]:
        post_index, reaction_index, upload_index = checkpoint
        return {
            "posts": self._posts[post_index:],
            "reactions": self._reaction_events[reaction_index:],
            "uploads": self._uploads[upload_index:],
            "thread": [
                {
                    key: value
                    for key, value in message.items()
                    if key in {"user", "bot_id", "text", "ts", "files"}
                }
                for message in self._messages
            ],
        }

    async def close(self) -> None:
        return None

    async def get_thread(
        self,
        channel: str,
        thread_ts: str,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self._validate_thread(channel, thread_ts)
        return self._messages[:limit]

    async def post_message(self, channel: str, thread_ts: str, text: str) -> str:
        self._validate_thread(channel, thread_ts)
        timestamp = self._next_timestamp()
        message = {
            "bot_id": "BLOCAL",
            "text": text,
            "ts": timestamp,
        }
        self._messages.append(message)
        self._posts.append({"text": text, "ts": timestamp})
        return timestamp

    async def add_reaction(self, channel: str, timestamp: str, emoji: str) -> None:
        self._validate_message(channel, timestamp)
        reactions = self._reactions.setdefault((channel, timestamp), set())
        if emoji not in reactions:
            reactions.add(emoji)
            self._reaction_events.append(
                {"action": "add", "message_ts": timestamp, "emoji": emoji}
            )

    async def remove_reaction(self, channel: str, timestamp: str, emoji: str) -> None:
        self._validate_message(channel, timestamp)
        reactions = self._reactions.setdefault((channel, timestamp), set())
        if emoji in reactions:
            reactions.remove(emoji)
            self._reaction_events.append(
                {"action": "remove", "message_ts": timestamp, "emoji": emoji}
            )

    async def file_info(self, file_id: str) -> dict[str, Any]:
        file = self._files.get(file_id)
        if file is None:
            raise SlackApiError("files.info", "file_not_found")
        return {key: value for key, value in file.items() if key != "content"}

    async def download(self, url: str, *, max_bytes: int) -> bytes:
        file_id = url.removeprefix("stub://")
        file = self._files.get(file_id)
        if file is None:
            raise SlackApiError("file.download", "file_not_found")
        content = bytes(file["content"])
        if len(content) > max_bytes:
            raise SlackApiError("file.download", f"file exceeds {max_bytes} bytes")
        return content

    async def upload_file(
        self,
        *,
        channel: str,
        thread_ts: str,
        path: Path,
        title: str,
        comment: str | None,
    ) -> dict[str, Any]:
        self._validate_thread(channel, thread_ts)
        content = await asyncio.to_thread(path.read_bytes)
        file_id = self._next_file_id()
        mimetype = "application/octet-stream"
        info = {
            "id": file_id,
            "name": path.name,
            "mimetype": mimetype,
            "size": len(content),
            "url_private_download": f"stub://{file_id}",
        }
        self._files[file_id] = {**info, "content": content}
        timestamp = self._next_timestamp()
        self._messages.append(
            {
                "bot_id": "BLOCAL",
                "text": comment or "",
                "ts": timestamp,
                "files": [
                    {key: info[key] for key in ("id", "name", "mimetype", "size")}
                ],
            }
        )
        upload = {
            "file_id": file_id,
            "name": path.name,
            "title": title,
            "comment": comment,
            "bytes": len(content),
            "content_base64": (
                base64.b64encode(content).decode("ascii")
                if len(content) <= MAX_RETURNED_UPLOAD_BYTES
                else None
            ),
            "content_omitted": len(content) > MAX_RETURNED_UPLOAD_BYTES,
        }
        self._uploads.append(upload)
        return {"file_id": file_id, "title": title}

    def _validate_thread(self, channel: str, thread_ts: str) -> None:
        if channel != "CLOCAL" or thread_ts != self._thread_ts:
            raise SlackApiError("stub", "message_not_found")

    def _validate_message(self, channel: str, timestamp: str) -> None:
        if channel != "CLOCAL" or not any(
            message["ts"] == timestamp for message in self._messages
        ):
            raise SlackApiError("stub", "message_not_found")
