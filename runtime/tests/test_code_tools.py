from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agents.tool_context import ToolContext

import slack_codex.tools.code_tools as code_tools
from slack_codex.tools.code_tools import (
    glob_files,
    list_directory,
    read_file,
    run_bash,
    run_bash_impl,
    write_file,
)


async def call_tool(tool: Any, arguments: dict[str, Any]) -> Any:
    raw_arguments = json.dumps(arguments)
    context = ToolContext(
        None,
        tool_name=tool.name,
        tool_call_id="call-1",
        tool_arguments=raw_arguments,
    )
    return await tool.on_invoke_tool(context, raw_arguments)


async def test_run_bash_returns_output(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    result = await run_bash_impl("printf 'hello'")
    assert result == {
        "exit_code": 0,
        "stdout": "hello",
        "stderr": "",
        "timed_out": False,
        "truncated": False,
    }


async def test_run_bash_preserves_configured_path(monkeypatch, tmp_path: Path) -> None:
    executable = tmp_path / "runtime-helper"
    executable.write_text("#!/bin/sh\nprintf available\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

    result = await run_bash_impl("runtime-helper")

    assert result["exit_code"] == 0
    assert result["stdout"] == "available"


async def test_run_bash_tracks_failures(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    invocation = SimpleNamespace(command_failures=0)
    arguments = json.dumps({"command": "exit 7", "timeout_seconds": 120})
    context = ToolContext(
        invocation,
        tool_name=run_bash.name,
        tool_call_id="call-1",
        tool_arguments=arguments,
    )

    result = await run_bash.on_invoke_tool(context, arguments)

    assert result["exit_code"] == 7
    assert invocation.command_failures == 1


async def test_run_bash_times_out_and_kills_process_group(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    result = await run_bash_impl("sleep 10", timeout_seconds=1)
    assert result["timed_out"] is True
    assert result["exit_code"] != 0


async def test_run_bash_truncates_large_output(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    result = await run_bash_impl("yes x | head -c 70000")
    assert result["truncated"] is True
    assert "output truncated" in result["stdout"]


async def test_dedicated_file_tools_reject_workspace_traversal(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))

    written = await call_tool(
        write_file,
        {"path": "../outside.txt", "content": "no"},
    )
    read = await call_tool(
        read_file,
        {"path": "../outside.txt", "start_line": 1, "end_line": None},
    )

    assert "inside" in written["error"]
    assert "inside" in read["error"]


async def test_file_listing_only_reports_truncation_when_results_are_omitted(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(code_tools, "MAX_RESULTS", 2)
    (tmp_path / "a.txt").touch()
    (tmp_path / "b.txt").touch()

    exact_list = await call_tool(list_directory, {"path": "."})
    exact_glob = await call_tool(glob_files, {"pattern": "*.txt", "path": "."})

    assert exact_list["truncated"] is False
    assert exact_glob["truncated"] is False

    (tmp_path / "c.txt").touch()
    truncated_list = await call_tool(list_directory, {"path": "."})
    truncated_glob = await call_tool(glob_files, {"pattern": "*.txt", "path": "."})

    assert truncated_list["truncated"] is True
    assert len(truncated_list["entries"]) == 2
    assert truncated_glob["truncated"] is True
    assert len(truncated_glob["matches"]) == 2


async def test_timeout_terminates_background_processes(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    result = await run_bash_impl(
        "sleep 30 & child=$!; printf \"$child\" > child.pid; wait \"$child\"",
        timeout_seconds=1,
    )
    child_pid = int((tmp_path / "child.pid").read_text(encoding="utf-8"))

    assert result["timed_out"] is True
    try:
        os.kill(child_pid, 0)
    except ProcessLookupError:
        pass
    else:
        raise AssertionError(f"child process {child_pid} survived command timeout")
