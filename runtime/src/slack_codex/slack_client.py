from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx


class SlackApiError(RuntimeError):
    def __init__(self, method: str, detail: str) -> None:
        super().__init__(f"{method}: {detail}")
        self.method = method
        self.detail = detail


class SlackClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://slack.com/api",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(30),
            follow_redirects=True,
        )

    @property
    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def close(self) -> None:
        await self._client.aclose()

    async def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(
                f"{self._base_url}/{method}",
                headers={**self.auth_headers, "Content-Type": "application/json; charset=utf-8"},
                json=payload,
            )
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SlackApiError(method, str(exc)) from exc
        if not result.get("ok"):
            raise SlackApiError(method, result.get("error", "unknown Slack error"))
        return result

    async def _get(self, method: str, params: dict[str, str]) -> dict[str, Any]:
        try:
            response = await self._client.get(
                f"{self._base_url}/{method}",
                headers=self.auth_headers,
                params=params,
            )
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SlackApiError(method, str(exc)) from exc
        if not result.get("ok"):
            raise SlackApiError(method, result.get("error", "unknown Slack error"))
        return result

    async def get_thread(
        self,
        channel: str,
        thread_ts: str,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        result = await self._get(
            "conversations.replies",
            {"channel": channel, "ts": thread_ts, "limit": str(limit)},
        )
        return list(result.get("messages", []))

    async def post_message(self, channel: str, thread_ts: str, text: str) -> str:
        result = await self._post(
            "chat.postMessage",
            {
                "channel": channel,
                "thread_ts": thread_ts,
                "text": text,
                "unfurl_links": False,
                "unfurl_media": False,
            },
        )
        return str(result.get("ts", ""))

    async def add_reaction(self, channel: str, timestamp: str, emoji: str) -> None:
        try:
            await self._post(
                "reactions.add",
                {"channel": channel, "timestamp": timestamp, "name": emoji},
            )
        except SlackApiError as exc:
            if exc.detail != "already_reacted":
                raise

    async def remove_reaction(self, channel: str, timestamp: str, emoji: str) -> None:
        try:
            await self._post(
                "reactions.remove",
                {"channel": channel, "timestamp": timestamp, "name": emoji},
            )
        except SlackApiError as exc:
            if exc.detail not in {"no_reaction", "message_not_found"}:
                raise

    async def file_info(self, file_id: str) -> dict[str, Any]:
        result = await self._get("files.info", {"file": file_id})
        file_data = result.get("file")
        if not isinstance(file_data, dict):
            raise SlackApiError("files.info", "response did not include a file")
        return file_data

    async def download(self, url: str, *, max_bytes: int) -> bytes:
        try:
            chunks: list[bytes] = []
            size = 0
            async with self._client.stream("GET", url, headers=self.auth_headers) as response:
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > max_bytes:
                    raise SlackApiError("file.download", f"file exceeds {max_bytes} bytes")
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise SlackApiError("file.download", f"file exceeds {max_bytes} bytes")
                    chunks.append(chunk)
            return b"".join(chunks)
        except SlackApiError:
            raise
        except httpx.HTTPError as exc:
            raise SlackApiError("file.download", str(exc)) from exc

    async def upload_file(
        self,
        *,
        channel: str,
        thread_ts: str,
        path: Path,
        title: str,
        comment: str | None,
    ) -> dict[str, Any]:
        size = (await _stat(path)).st_size
        reserve = await self._get(
            "files.getUploadURLExternal",
            {"filename": path.name, "length": str(size)},
        )
        upload_url = reserve.get("upload_url")
        file_id = reserve.get("file_id")
        if not upload_url or not file_id:
            raise SlackApiError(
                "files.getUploadURLExternal",
                "response did not include upload_url and file_id",
            )

        try:
            content = await _read_bytes(path)
            response = await self._client.post(
                str(upload_url),
                content=content,
                headers={"Content-Length": str(len(content))},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SlackApiError("file.upload", str(exc)) from exc

        payload: dict[str, Any] = {
            "files": [{"id": file_id, "title": title}],
            "channel_id": channel,
            "thread_ts": thread_ts,
        }
        if comment:
            payload["initial_comment"] = comment
        complete = await self._post("files.completeUploadExternal", payload)
        return {
            "file_id": file_id,
            "title": complete.get("files", [{}])[0].get("title", title),
        }


async def _read_bytes(path: Path) -> bytes:
    import asyncio

    return await asyncio.to_thread(path.read_bytes)


async def _stat(path: Path) -> Any:
    import asyncio

    return await asyncio.to_thread(path.stat)
