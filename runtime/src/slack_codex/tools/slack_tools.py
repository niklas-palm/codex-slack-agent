from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from agents import RunContextWrapper, function_tool

from slack_codex.models import InvocationContext, TerminalThreadStatus, ThreadStatus
from slack_codex.slack_client import SlackApiError
from slack_codex.workspace import resolve_workspace_path

MAX_FILE_BYTES = 50 * 1024 * 1024
STATUS_EMOJI = {
    "working": "large_yellow_circle",
    "waiting": "question",
    "done": "large_green_circle",
    "failed": "red_circle",
}
EMOJI_NAME = re.compile(r"^[a-zA-Z0-9_+-]{1,64}$")


def _error(exc: Exception, hint: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"error": str(exc)}
    if hint:
        result["hint"] = hint
    return result


def _targets(context: InvocationContext) -> list[str]:
    return list(
        dict.fromkeys(
            [
                context.slack.thread_ts,
                context.slack.trigger_message_ts,
            ]
        )
    )


async def set_thread_status_for_context(
    context: InvocationContext,
    status: ThreadStatus,
) -> dict[str, Any]:
    try:
        for timestamp in _targets(context):
            for emoji in STATUS_EMOJI.values():
                await context.slack_client.remove_reaction(
                    context.slack.channel_id,
                    timestamp,
                    emoji,
                )
            await context.slack_client.add_reaction(
                context.slack.channel_id,
                timestamp,
                STATUS_EMOJI[status],
            )
        context.status = status
        return {"success": True, "status": status, "timestamps": _targets(context)}
    except SlackApiError as exc:
        return _error(exc, "check reactions:write and that the bot can access the channel")


def _public_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for message in messages:
        item: dict[str, Any] = {
            "user": message.get("user") or message.get("bot_id") or "unknown",
            "text": message.get("text", ""),
            "ts": message.get("ts", ""),
        }
        files = message.get("files") or []
        if files:
            item["files"] = [
                {
                    "id": file.get("id"),
                    "name": file.get("name"),
                    "mimetype": file.get("mimetype"),
                    "size": file.get("size"),
                }
                for file in files
            ]
        result.append(item)
    return result


async def _thread_messages(context: InvocationContext, limit: int = 100) -> list[dict[str, Any]]:
    return await context.slack_client.get_thread(
        context.slack.channel_id,
        context.slack.thread_ts,
        limit=max(1, min(limit, 100)),
    )


async def _post_once(
    context: InvocationContext,
    kind: str,
    text: str,
) -> tuple[str, bool]:
    async with context.message_lock:
        key = (kind, text)
        if key in context.posted_messages:
            return context.posted_messages[key], True
        timestamp = await context.slack_client.post_message(
            context.slack.channel_id,
            context.slack.thread_ts,
            text,
        )
        context.posted_messages[key] = timestamp
        context.replied = True
        return timestamp, False


@function_tool
async def read_thread(
    run_context: RunContextWrapper[InvocationContext],
    limit: int = 100,
) -> dict[str, Any]:
    """Read the Slack thread that triggered this turn, oldest message first."""
    try:
        messages = await _thread_messages(run_context.context, limit)
        public = _public_messages(messages)
        return {"count": len(public), "messages": public}
    except SlackApiError as exc:
        return _error(exc, "check channels:history or groups:history and channel membership")


@function_tool
async def reply_to_thread(
    run_context: RunContextWrapper[InvocationContext],
    text: str,
) -> dict[str, Any]:
    """Post a Slack mrkdwn message in the triggering thread."""
    context = run_context.context
    try:
        if not text.strip():
            raise ValueError("text must not be empty")
        timestamp, duplicate = await _post_once(context, "reply", text)
        return {"success": True, "ts": timestamp, "duplicate": duplicate}
    except (SlackApiError, ValueError) as exc:
        return _error(exc, "check chat:write and channel membership")


@function_tool
async def set_thread_status(
    run_context: RunContextWrapper[InvocationContext],
    status: TerminalThreadStatus,
) -> dict[str, Any]:
    """Set the terminal status on the thread parent and triggering message.

    Use done after a successful reply or failed after explaining a failure.
    Eyes is retained.
    """
    return await set_thread_status_for_context(run_context.context, status)


@function_tool
async def ask_user(
    run_context: RunContextWrapper[InvocationContext],
    question: str,
) -> dict[str, Any]:
    """Ask the user a blocking question in Slack and mark the thread waiting.

    Calling this tool ends the current agent turn.
    """
    context = run_context.context
    try:
        if not question.strip():
            raise ValueError("question must not be empty")
        timestamp, duplicate = await _post_once(context, "question", question)
        context.waiting = True
        status_result = await set_thread_status_for_context(context, "waiting")
        return {
            "success": True,
            "ts": timestamp,
            "duplicate": duplicate,
            "status": status_result,
        }
    except (SlackApiError, ValueError) as exc:
        return _error(exc, "check Slack connectivity and bot scopes")


@function_tool
async def react_to_message(
    run_context: RunContextWrapper[InvocationContext],
    message_ts: str,
    emoji: str,
    remove: bool = False,
) -> dict[str, Any]:
    """Add or remove the bot's emoji reaction on a message in the triggering thread."""
    context = run_context.context
    try:
        if not EMOJI_NAME.fullmatch(emoji):
            raise ValueError("emoji must be a Slack emoji name such as white_check_mark")
        messages = await _thread_messages(context)
        valid_timestamps = {str(message.get("ts", "")) for message in messages}
        if message_ts not in valid_timestamps:
            raise ValueError("message_ts is not part of the triggering thread")
        action = (
            context.slack_client.remove_reaction
            if remove
            else context.slack_client.add_reaction
        )
        await action(context.slack.channel_id, message_ts, emoji)
        return {"success": True, "removed": remove}
    except (SlackApiError, ValueError) as exc:
        return _error(exc)


def _safe_filename(name: str) -> str:
    filename = Path(name).name
    if not filename or filename in {".", ".."}:
        raise ValueError("invalid filename")
    return filename


@function_tool
async def download_file(
    run_context: RunContextWrapper[InvocationContext],
    file_id: str,
    save_as: str | None = None,
) -> dict[str, Any]:
    """Download a file attached to the triggering Slack thread into /workspace."""
    context = run_context.context
    try:
        messages = await _thread_messages(context)
        attached_ids = {
            str(file.get("id"))
            for message in messages
            for file in message.get("files", [])
            if file.get("id")
        }
        if file_id not in attached_ids:
            raise ValueError("file_id is not attached to the triggering thread")

        info = await context.slack_client.file_info(file_id)
        size = int(info.get("size") or 0)
        if size > MAX_FILE_BYTES:
            raise ValueError(f"file exceeds {MAX_FILE_BYTES} bytes")
        url = info.get("url_private_download") or info.get("url_private")
        if not url:
            raise ValueError("Slack did not provide a private download URL")

        filename = _safe_filename(save_as or info.get("name") or f"{file_id}.bin")
        destination = resolve_workspace_path(context.workspace, filename)
        content = await context.slack_client.download(
            str(url),
            max_bytes=MAX_FILE_BYTES,
        )
        if b"<!doctype html" in content[:256].lower():
            raise ValueError("received HTML instead of the requested file")
        await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(destination.write_bytes, content)
        return {
            "success": True,
            "path": str(destination),
            "bytes": len(content),
            "mimetype": info.get("mimetype"),
        }
    except (SlackApiError, ValueError, OSError) as exc:
        return _error(exc, "check files:read, file access, and the destination filename")


@function_tool
async def upload_file(
    run_context: RunContextWrapper[InvocationContext],
    path: str,
    title: str | None = None,
    comment: str | None = None,
) -> dict[str, Any]:
    """Upload a file from /workspace to the triggering Slack thread."""
    context = run_context.context
    try:
        target = resolve_workspace_path(context.workspace, path, must_exist=True)
        if not target.is_file():
            raise ValueError("path is not a regular file")
        size = target.stat().st_size
        if size == 0:
            raise ValueError("file is empty")
        if size > MAX_FILE_BYTES:
            raise ValueError(f"file exceeds {MAX_FILE_BYTES} bytes")
        return await context.slack_client.upload_file(
            channel=context.slack.channel_id,
            thread_ts=context.slack.thread_ts,
            path=target,
            title=title or target.name,
            comment=comment,
        )
    except (SlackApiError, ValueError, OSError) as exc:
        return _error(exc, "check files:write and use a file inside /workspace")


SLACK_TOOLS = [
    read_thread,
    reply_to_thread,
    set_thread_status,
    ask_user,
    react_to_message,
    download_file,
    upload_file,
]
