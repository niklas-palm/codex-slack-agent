from __future__ import annotations

from pathlib import Path

MAX_TEXT_BYTES = 1_000_000


def resolve_workspace_path(
    workspace: Path,
    requested: str,
    *,
    must_exist: bool = False,
) -> Path:
    workspace = workspace.resolve()
    raw = Path(requested)
    candidate = raw if raw.is_absolute() else workspace / raw
    resolved = candidate.resolve()
    if resolved != workspace and not resolved.is_relative_to(workspace):
        raise ValueError(f"path must be inside {workspace}")
    if must_exist:
        resolved = resolved.resolve(strict=True)
        if resolved != workspace and not resolved.is_relative_to(workspace):
            raise ValueError(f"path must be inside {workspace}")
    return resolved


def read_text_file(path: Path) -> str:
    size = path.stat().st_size
    if size > MAX_TEXT_BYTES:
        raise ValueError(f"file is too large to read ({size} bytes, max {MAX_TEXT_BYTES})")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("file is not valid UTF-8 text") from exc
