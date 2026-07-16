from __future__ import annotations

from pathlib import Path

import pytest

from slack_codex.workspace import read_text_file, resolve_workspace_path


def test_resolve_workspace_path_rejects_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="inside"):
        resolve_workspace_path(tmp_path, "../outside.txt")


def test_resolve_workspace_path_accepts_nested_file(tmp_path: Path) -> None:
    target = resolve_workspace_path(tmp_path, "src/app.py")
    assert target == tmp_path / "src" / "app.py"


def test_read_text_file_rejects_binary(tmp_path: Path) -> None:
    target = tmp_path / "binary"
    target.write_bytes(b"\xff\xfe")
    with pytest.raises(ValueError, match="UTF-8"):
        read_text_file(target)
