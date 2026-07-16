from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import signal
import time
from pathlib import Path
from typing import Any

from agents import RunContextWrapper, function_tool

from slack_codex.models import InvocationContext
from slack_codex.workspace import (
    MAX_TEXT_BYTES,
    read_text_file,
    resolve_workspace_path,
)

MAX_COMMAND_OUTPUT = 64_000
MAX_COMMAND_TIMEOUT = 900
MAX_RESULTS = 500
logger = logging.getLogger(__name__)


def _workspace() -> Path:
    return Path(os.getenv("WORKSPACE_DIR", "/workspace")).resolve()


def _error(exc: Exception, hint: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"error": str(exc)}
    if hint:
        result["hint"] = hint
    return result


def _truncate(value: bytes) -> tuple[str, bool]:
    if len(value) <= MAX_COMMAND_OUTPUT:
        return value.decode("utf-8", errors="replace"), False
    half = MAX_COMMAND_OUTPUT // 2
    clipped = value[:half] + b"\n... output truncated ...\n" + value[-half:]
    return clipped.decode("utf-8", errors="replace"), True


async def _terminate_process_group(
    process: asyncio.subprocess.Process,
) -> tuple[bytes, bytes]:
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        return await asyncio.wait_for(process.communicate(), timeout=3)
    except TimeoutError:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return await process.communicate()


async def run_bash_impl(command: str, timeout_seconds: int = 120) -> dict[str, Any]:
    timeout_seconds = max(1, min(timeout_seconds, MAX_COMMAND_TIMEOUT))
    fingerprint = hashlib.sha256(command.encode("utf-8")).hexdigest()[:12]
    started = time.monotonic()
    logger.info(
        "run_bash started fingerprint=%s command_bytes=%d timeout_seconds=%d",
        fingerprint,
        len(command.encode("utf-8")),
        timeout_seconds,
    )
    workspace = _workspace()
    workspace.mkdir(parents=True, exist_ok=True)
    process = await asyncio.create_subprocess_exec(
        "bash",
        "-c",
        command,
        cwd=workspace,
        env=os.environ.copy(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        timed_out = True
        stdout, stderr = await _terminate_process_group(process)
    except asyncio.CancelledError:
        await _terminate_process_group(process)
        raise

    stdout_text, stdout_truncated = _truncate(stdout)
    stderr_text, stderr_truncated = _truncate(stderr)
    logger.info(
        "run_bash completed fingerprint=%s duration_seconds=%.3f exit_code=%s "
        "stdout_bytes=%d stderr_bytes=%d timed_out=%s",
        fingerprint,
        time.monotonic() - started,
        process.returncode,
        len(stdout),
        len(stderr),
        timed_out,
    )
    return {
        "exit_code": process.returncode,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "timed_out": timed_out,
        "truncated": stdout_truncated or stderr_truncated,
    }


@function_tool
async def run_bash(
    run_context: RunContextWrapper[InvocationContext],
    command: str,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Run a shell command in /workspace.

    Use for git, GitHub CLI, package management, builds, tests, and chained
    shell operations. The timeout is capped at 900 seconds.
    """
    result = await run_bash_impl(command, timeout_seconds)
    if result["exit_code"] != 0:
        run_context.context.command_failures += 1
    return result


@function_tool
async def read_file(path: str, start_line: int = 1, end_line: int | None = None) -> dict[str, Any]:
    """Read a UTF-8 text file inside /workspace, optionally selecting a line range."""
    try:
        target = resolve_workspace_path(_workspace(), path, must_exist=True)
        if not target.is_file():
            raise ValueError("path is not a file")
        text = await asyncio.to_thread(read_text_file, target)
        lines = text.splitlines(keepends=True)
        start = max(start_line, 1)
        end = len(lines) if end_line is None else max(end_line, start - 1)
        return {
            "path": str(target),
            "content": "".join(lines[start - 1 : end]),
            "start_line": start,
            "end_line": min(end, len(lines)),
            "total_lines": len(lines),
        }
    except Exception as exc:
        return _error(exc, "use a UTF-8 text file inside /workspace")


@function_tool
async def write_file(path: str, content: str) -> dict[str, Any]:
    """Create or overwrite a UTF-8 text file inside /workspace."""
    try:
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_TEXT_BYTES:
            raise ValueError(f"content exceeds {MAX_TEXT_BYTES} bytes")
        target = resolve_workspace_path(_workspace(), path)
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_text, content, encoding="utf-8")
        return {"success": True, "path": str(target), "bytes": len(encoded)}
    except Exception as exc:
        return _error(exc, "choose a path inside /workspace")


@function_tool
async def edit_file(
    path: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
) -> dict[str, Any]:
    """Replace exact text in a UTF-8 file inside /workspace.

    By default the old text must occur exactly once. Set replace_all only when
    every occurrence should change.
    """
    try:
        target = resolve_workspace_path(_workspace(), path, must_exist=True)
        text = await asyncio.to_thread(read_text_file, target)
        count = text.count(old_text)
        if count == 0:
            raise ValueError("old_text was not found")
        if count != 1 and not replace_all:
            raise ValueError(
                f"old_text occurs {count} times; provide a unique match or replace_all"
            )
        updated = text.replace(old_text, new_text, -1 if replace_all else 1)
        if len(updated.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ValueError(f"edited file exceeds {MAX_TEXT_BYTES} bytes")
        await asyncio.to_thread(target.write_text, updated, encoding="utf-8")
        return {"success": True, "path": str(target), "replacements": count if replace_all else 1}
    except Exception as exc:
        return _error(exc)


@function_tool
async def list_directory(path: str = ".") -> dict[str, Any]:
    """List the immediate contents of a directory inside /workspace."""
    try:
        target = resolve_workspace_path(_workspace(), path, must_exist=True)
        if not target.is_dir():
            raise ValueError("path is not a directory")
        items = sorted(target.iterdir(), key=lambda value: value.name)
        entries = []
        for item in items[:MAX_RESULTS]:
            stat = item.stat()
            entries.append(
                {
                    "name": item.name,
                    "type": "directory" if item.is_dir() else "file",
                    "size": stat.st_size if item.is_file() else None,
                }
            )
        return {"path": str(target), "entries": entries, "truncated": len(items) > MAX_RESULTS}
    except Exception as exc:
        return _error(exc)


@function_tool
async def glob_files(pattern: str, path: str = ".") -> dict[str, Any]:
    """Find files below a workspace directory using a glob pattern."""
    try:
        root = resolve_workspace_path(_workspace(), path, must_exist=True)
        if not root.is_dir():
            raise ValueError("path is not a directory")
        matches: list[str] = []
        for item in root.glob(pattern):
            resolved = item.resolve()
            if resolved.is_file() and resolved.is_relative_to(_workspace()):
                matches.append(str(resolved.relative_to(_workspace())))
                if len(matches) > MAX_RESULTS:
                    break
        return {
            "matches": sorted(matches)[:MAX_RESULTS],
            "truncated": len(matches) > MAX_RESULTS,
        }
    except Exception as exc:
        return _error(exc)


@function_tool
async def grep_search(
    query: str,
    path: str = ".",
    glob: str | None = None,
    max_results: int = 200,
) -> dict[str, Any]:
    """Search text files under a workspace path with ripgrep."""
    try:
        target = resolve_workspace_path(_workspace(), path, must_exist=True)
        limit = max(1, min(max_results, MAX_RESULTS))
        command = ["rg", "--line-number", "--color", "never"]
        if glob:
            command.extend(["--glob", glob])
        command.extend(["--", query, str(target)])
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=_workspace(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        lines = stdout.decode("utf-8", errors="replace").splitlines()
        return {
            "matches": lines[:limit],
            "truncated": len(lines) > limit,
            "exit_code": process.returncode,
            "stderr": stderr.decode("utf-8", errors="replace"),
        }
    except Exception as exc:
        return _error(exc, "ripgrep must be installed and path must be inside /workspace")


CODE_TOOLS = [
    run_bash,
    read_file,
    write_file,
    edit_file,
    list_directory,
    glob_files,
    grep_search,
]
