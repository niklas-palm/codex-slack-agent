from __future__ import annotations

import asyncio
import base64
import inspect
import logging
import os
import subprocess
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from agents import Agent, RunContextWrapper, RunHooks, Runner, TResponseInputItem
from agents.mcp import MCPServer
from agents.tool import Tool
from openai import AsyncOpenAI

from slack_codex.agent import build_agent, model_for_parent_prompt
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
from slack_codex.web_fetch import WebFetcher

logger = logging.getLogger(__name__)
MAX_PROCESSED_EVENTS = 1024
SUPPORTED_IMAGE_MIME_TYPES = {"image/gif", "image/jpeg", "image/png", "image/webp"}
MAX_IMAGE_BYTES = 3_750_000


def _image_data_url(content: bytes, mimetype: str) -> str:
    """Return a validated image as a data URL accepted by the Responses API."""
    signatures = {
        "image/gif": (b"GIF87a", b"GIF89a"),
        "image/jpeg": (b"\xff\xd8\xff",),
        "image/png": (b"\x89PNG\r\n\x1a\n",),
        "image/webp": (b"RIFF",),
    }
    if mimetype not in signatures:
        raise ValueError(f"unsupported image type: {mimetype}")
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise ValueError(f"image must be between 1 and {MAX_IMAGE_BYTES} bytes")
    if not any(content.startswith(signature) for signature in signatures[mimetype]):
        raise ValueError(f"file content does not match {mimetype}")
    if mimetype == "image/webp" and content[8:12] != b"WEBP":
        raise ValueError("file content does not match image/webp")
    encoded = base64.b64encode(content).decode("ascii")
    return f"data:{mimetype};base64,{encoded}"


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
    web_fetcher: WebFetcher | None = None
    web_search_server: MCPServer | None = None
    started: bool = False

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

        web_fetcher = WebFetcher()
        openai_client, agent, web_search_server = build_agent(settings, web_fetcher)
        return cls(
            settings=settings,
            openai_client=openai_client,
            agent=agent,
            slack_client=SlackClient(slack_token),
            web_fetcher=web_fetcher,
            web_search_server=web_search_server,
        )

    @classmethod
    def create_local(cls, settings: Settings) -> RuntimeState:
        """Build the real model and web tools without loading Slack or GitHub secrets."""

        settings.workspace.mkdir(parents=True, exist_ok=True)
        os.environ["GH_REPO"] = settings.github_repository
        os.environ["WORKSPACE_DIR"] = str(settings.workspace)
        web_fetcher = WebFetcher()
        openai_client, agent, web_search_server = build_agent(settings, web_fetcher)
        return cls(
            settings=settings,
            openai_client=openai_client,
            agent=agent,
            slack_client=StubSlackClient(),  # type: ignore[arg-type]
            web_fetcher=web_fetcher,
            web_search_server=web_search_server,
        )

    async def start(self) -> None:
        if self.started:
            return
        if self.web_search_server is None:
            raise RuntimeError("Web Search Gateway server is not configured")

        try:
            await self.web_search_server.connect()
            tools = await self.web_search_server.list_tools()
        except BaseException:
            await self.web_search_server.cleanup()
            raise
        if not tools:
            await self.web_search_server.cleanup()
            raise RuntimeError("Web Search Gateway did not expose any tools")

        self.started = True
        logger.info(
            "Connected Web Search Gateway with tools: %s",
            ", ".join(tool.name for tool in tools),
        )

    async def close(self) -> None:
        self.started = False
        errors: list[BaseException] = []

        if self.web_search_server is not None:
            try:
                await self.web_search_server.cleanup()
            except BaseException as exc:
                errors.append(exc)
                logger.exception("Failed to close Web Search Gateway")
        if self.web_fetcher is not None:
            try:
                await self.web_fetcher.close()
            except BaseException as exc:
                errors.append(exc)
                logger.exception("Failed to close web fetcher")
        try:
            await self.slack_client.close()
        except BaseException as exc:
            errors.append(exc)
            logger.exception("Failed to close Slack client")

        close_client = getattr(self.openai_client, "close", None)
        if callable(close_client):
            try:
                result = close_client()
                if inspect.isawaitable(result):
                    await result
            except BaseException as exc:
                errors.append(exc)
                logger.exception("Failed to close Bedrock client")

        if errors:
            raise errors[0]

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
                "command_failures": context.command_failures,
                "tool_calls": context.tool_calls,
                "slack": client.snapshot(checkpoint),
            }

    async def _input_for_slack_turn(
        self, payload: InvocationPayload, slack_client: SlackClient
    ) -> dict[str, Any]:
        """Build Responses API input from images attached to the triggering message."""
        messages = await slack_client.get_thread(
            payload.slack.channel_id, payload.slack.thread_ts, limit=100
        )
        trigger = next(
            (
                message
                for message in messages
                if message.get("ts") == payload.slack.trigger_message_ts
            ),
            None,
        )
        content: list[dict[str, Any]] = [{"type": "input_text", "text": payload.prompt}]
        if trigger is None:
            return {"role": "user", "content": payload.prompt}

        for attachment in trigger.get("files", []):
            mimetype = str(attachment.get("mimetype", "")).lower()
            size = attachment.get("size")
            file_id = attachment.get("id")
            if (
                mimetype not in SUPPORTED_IMAGE_MIME_TYPES
                or not isinstance(size, int)
                or not 0 < size <= MAX_IMAGE_BYTES
                or not isinstance(file_id, str)
                or not file_id
            ):
                continue
            info = await slack_client.file_info(file_id)
            url = info.get("url_private_download") or info.get("url_private")
            if not isinstance(url, str) or not url:
                continue
            image = await slack_client.download(url, max_bytes=MAX_IMAGE_BYTES)
            content.append(
                {
                    "type": "input_image",
                    "detail": "auto",
                    "image_url": _image_data_url(image, mimetype),
                }
            )

        if len(content) == 1:
            return {"role": "user", "content": payload.prompt}
        return {"role": "user", "content": content}

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
        if payload.slack.trigger_message_ts == payload.slack.thread_ts:
            self.agent = self.agent.clone(model=model_for_parent_prompt(payload.prompt))
        working = await set_thread_status_for_context(context, "working")
        if working.get("error"):
            logger.warning("Failed to set working status: %s", working["error"])
        user_input: TResponseInputItem = await self._input_for_slack_turn(payload, slack_client)  # type: ignore[assignment]
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

        is_waiting = context.waiting and context.status == "waiting"
        is_terminal = not context.waiting and context.status in {"done", "failed"}
        if not context.replied or not (is_waiting or is_terminal):
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
            "!/app/.venv/bin/github-app-credential",
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
