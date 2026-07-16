from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from agents.tool_context import ToolContext

from slack_codex.tools.code_tools import read_file, run_bash_impl, write_file


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
