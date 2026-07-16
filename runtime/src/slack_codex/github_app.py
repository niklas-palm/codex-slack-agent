from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

GITHUB_API = "https://api.github.com"


@dataclass(frozen=True)
class GitHubAppCredentials:
    app_id: str
    installation_id: str
    private_key: str

    @classmethod
    def from_secret(cls, value: str) -> GitHubAppCredentials:
        try:
            data = json.loads(value)
        except json.JSONDecodeError as exc:
            raise RuntimeError("GitHub App credentials secret must contain JSON") from exc
        if not isinstance(data, dict):
            raise RuntimeError("GitHub App credentials secret must contain a JSON object")

        fields = {}
        for name in ("app_id", "installation_id", "private_key"):
            raw = data.get(name)
            fields[name] = raw.strip() if isinstance(raw, str) else ""
        missing = [name for name, field in fields.items() if not field]
        if missing:
            raise RuntimeError(
                f"GitHub App credentials secret is missing: {', '.join(missing)}"
            )
        return cls(**fields)

    @classmethod
    def from_env(cls) -> GitHubAppCredentials:
        return cls(
            app_id=_required_env("GH_APP_ID"),
            installation_id=_required_env("GH_APP_INSTALLATION_ID"),
            private_key=_required_env("GH_APP_PRIVATE_KEY"),
        )

    def export(self) -> None:
        os.environ["GH_APP_ID"] = self.app_id
        os.environ["GH_APP_INSTALLATION_ID"] = self.installation_id
        os.environ["GH_APP_PRIVATE_KEY"] = self.private_key


def mint_installation_token(
    credentials: GitHubAppCredentials,
    *,
    client: httpx.Client | None = None,
    now: int | None = None,
) -> str:
    issued_at = int(time.time()) if now is None else now
    app_jwt = jwt.encode(
        {
            "iat": issued_at - 60,
            "exp": issued_at + 540,
            "iss": credentials.app_id,
        },
        credentials.private_key,
        algorithm="RS256",
    )
    request_client = client or httpx.Client(timeout=30)
    try:
        response = request_client.post(
            (
                f"{GITHUB_API}/app/installations/"
                f"{credentials.installation_id}/access_tokens"
            ),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {app_jwt}",
                "User-Agent": "slack-codex-agent",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        response.raise_for_status()
        payload: Any = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError(f"Could not mint GitHub App installation token: {exc}") from exc
    finally:
        if client is None:
            request_client.close()

    token = payload.get("token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError("GitHub App token response did not contain a token")
    return token


def token_main() -> None:
    print(mint_installation_token(GitHubAppCredentials.from_env()))


def credential_main() -> None:
    operation = sys.argv[1] if len(sys.argv) > 1 else "get"
    if operation != "get":
        return
    sys.stdin.read()
    token = mint_installation_token(GitHubAppCredentials.from_env())
    print("protocol=https")
    print("host=github.com")
    print("username=x-access-token")
    print(f"password={token}")
    print()


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value
