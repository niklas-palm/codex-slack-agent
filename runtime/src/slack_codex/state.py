from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from agents import Agent, RunContextWrapper, RunHooks, Runner, TResponseInputItem
from agents.tool import Tool
from openai import AsyncOpenAI

from slack_codex.agent import build_agent
from slack_codex.github_app import GitHubAppCredentials
from slack_codex.models import (
    InvocationContext,
    InvocationPayload,
    TestInvocationPayload,
)
from slack_codex.secrets import SecretLoader
from slack_codex.settings import Settings
from slack_codex.slack_client import SlackClient
from slack_codex.test_slack_client import StubSlackClient
from slack_codex.tools.slack_tools import set_thread_status_for_context

logger = logging.getLogger(__name__)
MAX_PROCESSED_EVENTS = 1024


class RuntimeHooks(RunHooks[InvocationContext]):
    async def on_tool_start(
        self,
        context: RunContextWrapper[InvocationContext],
        _agent: Agent[InvocationContext],
        tool: Tool,
    ) -> None:
        count = context.context.tool_calls.get(tool.name, 0) + 1
        context.context.tool_calls[tool.name] = count
        logger.info("Tool started: %s call=%d", tool.name, count)

    async def on_tool_end(
        self,
        _context: RunContextWrapper[InvocationContext],
        _agent: Agent[InvocationContext],
        tool: Tool,
        _result: object,
    ) -> None:
        logger.info("Tool completed: %s", tool.name)


RUNTIME_HOOKS = RuntimeHooks()


class EventDeduplicator:
    def __init__(self, max_size: int = MAX_PROCESSED_EVENTS) -> None:
        self._max_size = max_size
        self._order: deque[str] = deque()
        self._seen: set[str] = set()
        self._lock = threading.Lock()

    def add(self, event_id: str) -> bool:
        with self._lock:
            if event_id in self._seen:
                return False
            self._seen.add(event_id)
            self._order.append(event_id)
            while len(self._order) > self._max_size:
                self._seen.discard(self._order.popleft())
            return True


@dataclass
class RuntimeState:
    settings: Settings
    openai_client: AsyncOpenAI
    agent: Any
    slack_client: SlackClient
    runner: Any = Runner
    history: list[TResponseInputItem] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    events: EventDeduplicator = field(default_factory=EventDeduplicator)
    session_id: str | None = None
    test_slack_client: StubSlackClient | None = None

    @classmethod
    def create(cls, settings: Settings) -> RuntimeState:
        secrets = SecretLoader(settings.aws_region)
        slack_token = secrets.get(settings.slack_bot_token_secret_arn)
        github_credentials = GitHubAppCredentials.from_secret(
            secrets.get(settings.github_app_credentials_secret_arn)
        )
        github_credentials.export()
        os.environ["GH_REPO"] = settings.github_repository
        os.environ["WORKSPACE_DIR"] = str(settings.workspace)
        settings.workspace.mkdir(parents=True, exist_ok=True)
        _configure_git()

        openai_client, agent = build_agent(settings)
        return cls(
            settings=settings,
            openai_client=openai_client,
            agent=agent,
            slack_client=SlackClient(slack_token),
        )

    def bind_session(self, session_id: str | None) -> None:
        if not session_id:
            raise ValueError("AgentCore invocation did not include a runtime session ID")
        if self.session_id is None:
            self.session_id = session_id
        elif self.session_id != session_id:
            raise RuntimeError(
                f"microVM is already bound to session {self.session_id}, received {session_id}"
            )

    async def run(self, payload: InvocationPayload) -> None:
        async with self.lock:
            await self._run_locked(payload, self.slack_client)

    async def run_test(self, payload: TestInvocationPayload) -> dict[str, Any]:
        async with self.lock:
            if self.test_slack_client is None:
                self.test_slack_client = StubSlackClient()
            client = self.test_slack_client
            checkpoint = client.checkpoint()
            try:
                slack = client.start_turn(
                    payload.prompt,
                    payload.user_id,
                    payload.attachments,
                )
            except Exception as exc:
                return {
                    "status": "failed",
                    "event_id": payload.event_id,
                    "error": str(exc),
                    "slack": client.snapshot(checkpoint),
                }
            invocation = InvocationPayload(
                source="slack",
                event_id=payload.event_id,
                prompt=payload.prompt,
                slack=slack,
            )
            try:
                context = await self._run_locked(invocation, client)
            except Exception as exc:
                return {
                    "status": "failed",
                    "event_id": payload.event_id,
                    "error": str(exc),
                    "slack": client.snapshot(checkpoint),
                }
            return {
                "status": "completed",
                "event_id": payload.event_id,
                "thread_status": context.status,
                "replied": context.replied,
                "waiting": context.waiting,
                "slack": client.snapshot(checkpoint),
            }

    async def _run_locked(
        self,
        payload: InvocationPayload,
        slack_client: SlackClient,
    ) -> InvocationContext:
        context = InvocationContext(
            slack=payload.slack,
            slack_client=slack_client,
            workspace=self.settings.workspace,
        )
        working = await set_thread_status_for_context(context, "working")
        if working.get("error"):
            logger.warning("Failed to set working status: %s", working["error"])
        user_input: TResponseInputItem = {
            "role": "user",
            "content": payload.prompt,
        }
        self.history.append(user_input)
        try:
            result = await self.runner.run(
                self.agent,
                self.history,
                context=context,
                max_turns=1000,
                hooks=RUNTIME_HOOKS,
            )
            self.history = result.to_input_list()
        except Exception:
            await self._fallback(context)
            raise

        if not context.replied and not context.waiting:
            await self._fallback(context)
        return context

    async def _fallback(self, context: InvocationContext) -> None:
        try:
            await context.slack_client.post_message(
                context.slack.channel_id,
                context.slack.thread_ts,
                ":warning: I couldn't finish that request. Please @codex me again to retry.",
            )
            context.replied = True
        except Exception:
            logger.exception("Failed to post fallback Slack reply")
        status = await set_thread_status_for_context(context, "failed")
        if status.get("error"):
            logger.error("Failed to set failure status: %s", status["error"])


def _configure_git() -> None:
    commands = [
        ["git", "config", "--global", "user.name", "Codex Slack Agent"],
        ["git", "config", "--global", "user.email", "codex-agent@users.noreply.github.com"],
        [
            "git",
            "config",
            "--global",
            "credential.https://github.com.helper",
            "github-app-credential",
        ],
    ]
    for command in commands:
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"Failed to configure git command {command[0]}") from exc
