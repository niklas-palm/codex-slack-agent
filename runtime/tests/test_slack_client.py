from __future__ import annotations

import httpx
import pytest

from slack_codex.slack_client import SlackApiError, SlackClient


async def test_download_rejects_content_larger_than_limit() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"12345")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SlackClient("token", client=http_client)
    try:
        with pytest.raises(SlackApiError, match="exceeds 4 bytes"):
            await client.download("https://files.example/file", max_bytes=4)
    finally:
        await client.close()


async def test_slack_api_error_includes_method_and_detail() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "not_in_channel"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SlackClient("token", client=http_client)
    try:
        with pytest.raises(SlackApiError, match="chat.postMessage: not_in_channel"):
            await client.post_message("C1", "1.0", "hello")
    finally:
        await client.close()
