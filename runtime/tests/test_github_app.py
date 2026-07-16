from __future__ import annotations

import httpx
import pytest

import slack_codex.github_app as github_app
from slack_codex.github_app import GitHubAppCredentials, mint_installation_token


def test_credentials_are_loaded_from_one_json_secret(monkeypatch) -> None:
    credentials = GitHubAppCredentials.from_secret(
        '{"app_id":"123","installation_id":"456","private_key":"key"}'
    )
    credentials.export()

    assert GitHubAppCredentials.from_env() == credentials
    assert github_app.os.environ["GH_APP_PRIVATE_KEY"] == "key"


def test_credentials_reject_incomplete_secret() -> None:
    with pytest.raises(RuntimeError, match="installation_id, private_key"):
        GitHubAppCredentials.from_secret(
            '{"app_id":"123","installation_id":null,"private_key":{}}'
        )


def test_mints_installation_token_without_exposing_credentials(monkeypatch) -> None:
    request: httpx.Request | None = None

    def handler(incoming: httpx.Request) -> httpx.Response:
        nonlocal request
        request = incoming
        return httpx.Response(201, json={"token": "installation-token"})

    monkeypatch.setattr(github_app.jwt, "encode", lambda *_args, **_kwargs: "app-jwt")
    credentials = GitHubAppCredentials("123", "456", "private-key")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        token = mint_installation_token(credentials, client=client, now=1_800_000_000)

    assert token == "installation-token"
    assert request is not None
    assert request.url.path == "/app/installations/456/access_tokens"
    assert request.headers["authorization"] == "Bearer app-jwt"
