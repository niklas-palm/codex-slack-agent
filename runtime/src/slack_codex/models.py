from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from slack_codex.slack_client import SlackClient

ThreadStatus = Literal["working", "waiting", "done", "failed"]
TerminalThreadStatus = Literal["done", "failed"]


class SlackInvocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    team_id: str = Field(min_length=1)
    channel_id: str = Field(min_length=1)
    thread_ts: str = Field(min_length=1)
    trigger_message_ts: str = Field(min_length=1)
    slack_user_id: str = Field(min_length=1)


class InvocationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["slack"]
    event_id: str = Field(min_length=1)
    prompt: str
    slack: SlackInvocation


class TestAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(min_length=1, max_length=7_000_000)
    mimetype: str = Field(default="application/octet-stream", min_length=1)


class TestInvocationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["test"]
    event_id: str = Field(min_length=1)
    prompt: str
    user_id: str = Field(default="local-user", min_length=1)
    attachments: list[TestAttachment] = Field(default_factory=list, max_length=10)


@dataclass
class InvocationContext:
    slack: SlackInvocation
    slack_client: SlackClient
    workspace: Path
    replied: bool = False
    waiting: bool = False
    status: ThreadStatus | None = None
    message_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    posted_messages: dict[tuple[str, str], str] = field(default_factory=dict)
    tool_calls: dict[str, int] = field(default_factory=dict)
    command_failures: int = 0
