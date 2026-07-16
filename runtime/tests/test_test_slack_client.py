from __future__ import annotations

import base64
from pathlib import Path

from slack_codex.models import TestAttachment as StubAttachment
from slack_codex.test_slack_client import StubSlackClient


async def test_stub_slack_client_supports_thread_files_and_uploads(
    tmp_path: Path,
) -> None:
    client = StubSlackClient()
    attachment = StubAttachment(
        name="input.txt",
        content_base64=base64.b64encode(b"input").decode("ascii"),
        mimetype="text/plain",
    )
    checkpoint = client.checkpoint()
    slack = client.start_turn("inspect this", "local-user", [attachment])

    thread = await client.get_thread("CLOCAL", slack.thread_ts)
    file_id = thread[0]["files"][0]["id"]
    info = await client.file_info(file_id)
    downloaded = await client.download(info["url_private_download"], max_bytes=100)
    output = tmp_path / "output.txt"
    output.write_text("result", encoding="utf-8")
    uploaded = await client.upload_file(
        channel="CLOCAL",
        thread_ts=slack.thread_ts,
        path=output,
        title="Output",
        comment="Generated",
    )
    snapshot = client.snapshot(checkpoint)

    assert downloaded == b"input"
    assert uploaded["title"] == "Output"
    assert snapshot["reactions"] == [
        {"action": "add", "message_ts": "1.000000", "emoji": "eyes"}
    ]
    assert snapshot["uploads"][0]["content_base64"] == base64.b64encode(
        b"result"
    ).decode("ascii")
