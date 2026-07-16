from __future__ import annotations

import socket
from collections.abc import Awaitable, Callable

import httpx

from slack_codex.web_fetch import MAX_BODY_BYTES, MAX_EXTRACTED_CHARACTERS, WebFetcher

Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


async def public_resolver(_host: str) -> list[str]:
    return ["93.184.216.34"]


def make_fetcher(
    handler: Handler,
    *,
    resolver: Callable[[str], Awaitable[list[str]]] = public_resolver,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> tuple[WebFetcher, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    fetcher = WebFetcher(
        client=client,
        resolver=resolver,
        sleep=sleep or (lambda _seconds: _no_sleep()),
    )
    return fetcher, client


async def _no_sleep() -> None:
    return None


async def test_fetch_rejects_non_public_or_unsafe_urls() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="unused")

    fetcher, client = make_fetcher(handler)
    try:
        for url in [
            "http://example.com",
            "https://user:password@example.com",
            "https://localhost",
            "https://127.0.0.1",
            "https://example.com:444",
        ]:
            result = await fetcher.fetch(url)
            assert result["error"]["code"] == "blocked_url"
        assert calls == []
    finally:
        await client.aclose()


async def test_fetch_rejects_private_dns_addresses_and_resolution_failures() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("blocked URLs must not be requested")

    async def private_resolver(_host: str) -> list[str]:
        return ["10.0.0.12"]

    async def failing_resolver(_host: str) -> list[str]:
        raise socket.gaierror("not found")

    private_fetcher, private_client = make_fetcher(handler, resolver=private_resolver)
    failing_fetcher, failing_client = make_fetcher(handler, resolver=failing_resolver)
    try:
        private = await private_fetcher.fetch("https://private.example")
        failed = await failing_fetcher.fetch("https://missing.example")
        assert private == {
            "error": {
                "code": "blocked_url",
                "message": "Private, local, and reserved network addresses are not allowed",
            }
        }
        assert failed == {
            "error": {
                "code": "blocked_url",
                "message": "Hostname could not be resolved",
            }
        }
    finally:
        await private_client.aclose()
        await failing_client.aclose()


async def test_fetch_revalidates_every_redirect_target() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://private.example/internal"})

    async def resolver(host: str) -> list[str]:
        return ["93.184.216.34"] if host == "public.example" else ["192.168.1.2"]

    fetcher, client = make_fetcher(handler, resolver=resolver)
    try:
        result = await fetcher.fetch("https://public.example/start")
        assert result["error"]["code"] == "blocked_url"
        assert calls == ["https://public.example/start"]
    finally:
        await client.aclose()


async def test_fetch_enforces_redirect_limit_and_per_host_rate_limit() -> None:
    calls: list[str] = []
    delays: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": f"/redirect-{len(calls)}"})

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    fetcher, client = make_fetcher(handler, sleep=sleep)
    try:
        result = await fetcher.fetch("https://example.com/start")
        assert result["error"]["code"] == "too_many_redirects"
        assert len(calls) == 6
        assert len(delays) == 5
        assert all(delay > 0 for delay in delays)
    finally:
        await client.aclose()


async def test_fetch_returns_structured_transport_and_response_errors() -> None:
    async def timeout_handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    async def status_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(304)

    timeout_fetcher, timeout_client = make_fetcher(timeout_handler)
    status_fetcher, status_client = make_fetcher(status_handler)
    try:
        timeout = await timeout_fetcher.fetch("https://example.com/slow")
        status = await status_fetcher.fetch("https://example.com/stale")
        assert timeout["error"]["code"] == "timeout"
        assert status == {
            "error": {
                "code": "http_error",
                "message": "304 while fetching https://example.com/stale",
            }
        }
    finally:
        await timeout_client.aclose()
        await status_client.aclose()


async def test_fetch_rejects_unsupported_content_and_oversized_responses() -> None:
    async def media_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")

    async def large_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "text/plain",
                "content-length": str(MAX_BODY_BYTES + 1),
            },
        )

    media_fetcher, media_client = make_fetcher(media_handler)
    large_fetcher, large_client = make_fetcher(large_handler)
    try:
        media = await media_fetcher.fetch("https://example.com/file")
        large = await large_fetcher.fetch("https://example.com/large")
        assert media["error"]["code"] == "unsupported_content_type"
        assert large["error"]["code"] == "response_too_large"
    finally:
        await media_client.aclose()
        await large_client.aclose()


async def test_fetch_extracts_clean_markdown_metadata_and_tables_without_cookies() -> None:
    html = """
    <!doctype html>
    <html>
      <head>
        <title>Ignored browser title</title>
        <meta property="og:title" content="Example article">
        <meta name="author" content="Example author">
        <meta property="article:published_time" content="2026-07-16">
      </head>
      <body>
        <nav><a href="/login">Sign in</a></nav>
        <main>
          <h1>Example article</h1>
          <p>This is readable server-rendered text.</p>
          <table><tr><th>Name</th><th>Value</th></tr><tr><td>one</td><td>1</td></tr></table>
        </main>
      </body>
    </html>
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["cookie"] == ""
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=html,
        )

    fetcher, client = make_fetcher(handler)
    try:
        result = await fetcher.fetch("https://example.com/article#section")
        assert result["url"] == "https://example.com/article"
        assert result["title"] == "Example article"
        assert result["published_date"] == "2026-07-16"
        assert result["content_type"] == "text/html"
        assert "This is readable server-rendered text." in result["content"]
        assert "Name" in result["content"]
        assert "Sign in" not in result["content"]
        assert result["truncated"] is False
        assert set(result) == {
            "url",
            "title",
            "published_date",
            "content_type",
            "content",
            "truncated",
        }
    finally:
        await client.aclose()


async def test_fetch_reports_empty_pages_and_truncates_plain_text() -> None:
    async def empty_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html></html>")

    async def text_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/plain; charset=utf-8"},
            text="x" * (MAX_EXTRACTED_CHARACTERS + 100),
        )

    empty_fetcher, empty_client = make_fetcher(empty_handler)
    text_fetcher, text_client = make_fetcher(text_handler)
    try:
        empty = await empty_fetcher.fetch("https://example.com/empty")
        text = await text_fetcher.fetch("https://example.com/long")
        assert empty["error"]["code"] == "no_readable_content"
        assert text["truncated"] is True
        assert text["content"].startswith("x" * MAX_EXTRACTED_CHARACTERS)
        assert text["content"].endswith("[Content truncated]")
    finally:
        await empty_client.aclose()
        await text_client.aclose()
