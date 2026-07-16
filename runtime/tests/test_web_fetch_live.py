from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from slack_codex.web_fetch import WebFetcher

LIVE_SITES = {
    "aws-docs": "https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-connector-web-search-tool.html",
    "python-docs": "https://docs.python.org/3/",
    "python-site": "https://www.python.org/",
    "mdn": "https://developer.mozilla.org/en-US/docs/Web/HTTP",
    "rfc-editor": "https://www.rfc-editor.org/",
    "kubernetes": "https://kubernetes.io/docs/home/",
    "go": "https://go.dev/doc/",
    "rust": "https://www.rust-lang.org/learn",
    "react": "https://react.dev/",
    "fastapi": "https://fastapi.tiangolo.com/",
    "pydantic": "https://docs.pydantic.dev/latest/",
    "trafilatura": "https://trafilatura.readthedocs.io/en/latest/",
    "httpx": "https://www.python-httpx.org/",
    "w3c": "https://www.w3.org/",
    "wikipedia": "https://en.wikipedia.org/wiki/Web_scraping",
    "github": "https://github.com/",
    "pypi": "https://pypi.org/",
    "nasa": "https://www.nasa.gov/",
    "npr": "https://www.npr.org/",
    "guardian": "https://www.theguardian.com/international",
    "bbc": "https://www.bbc.com/",
    "ars-technica": "https://arstechnica.com/",
    "mozilla": "https://www.mozilla.org/",
    "cloudflare": "https://www.cloudflare.com/",
}


@pytest.mark.live_web_fetch
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_WEB_FETCH") != "1",
    reason="set RUN_LIVE_WEB_FETCH=1 to run public-site extraction checks",
)
async def test_live_public_site_extraction() -> None:
    fetcher = WebFetcher()
    try:
        results = await asyncio.gather(
            *(fetcher.fetch(url) for url in LIVE_SITES.values()),
            return_exceptions=True,
        )
    finally:
        await fetcher.close()

    readable = 0
    for (name, requested_url), result in zip(LIVE_SITES.items(), results, strict=True):
        if isinstance(result, Exception):
            print(
                f"{name}: url={requested_url} final_url=- status=exception "
                f"title=- extracted_length=0 truncated=- error={result}"
            )
            continue

        error: dict[str, Any] | None = result.get("error")
        if error is not None:
            print(
                f"{name}: url={requested_url} final_url=- status={error['code']} "
                f"title=- extracted_length=0 truncated=- error={error['message']}"
            )
            continue

        readable += 1
        print(
            f"{name}: url={requested_url} final_url={result['url']} status=readable "
            f"title={result['title'] or '-'} extracted_length={len(result['content'])} "
            f"truncated={result['truncated']} error=-"
        )

    assert readable >= 20, f"Expected at least 20 readable pages, got {readable}"
