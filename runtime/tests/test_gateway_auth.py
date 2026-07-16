from __future__ import annotations

import httpx
from botocore.credentials import ReadOnlyCredentials

from slack_codex.gateway_auth import AgentCoreGatewaySigV4Auth


class FakeCredentialProvider:
    def get_frozen_credentials(self) -> ReadOnlyCredentials:
        return ReadOnlyCredentials(
            access_key="AKIDEXAMPLE",
            secret_key="secret",
            token="session-token",
        )


class FakeSession:
    def get_credentials(self) -> FakeCredentialProvider:
        return FakeCredentialProvider()


def test_gateway_auth_signs_every_request_with_agentcore_service() -> None:
    request = httpx.Request(
        "POST",
        "https://gateway.example/mcp",
        content=b'{"jsonrpc":"2.0"}',
    )
    auth = AgentCoreGatewaySigV4Auth("us-east-1", session=FakeSession())

    signed = next(auth.auth_flow(request))

    assert signed.headers["authorization"].startswith(
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/"
    )
    assert "bedrock-agentcore/aws4_request" in signed.headers["authorization"]
    assert signed.headers["x-amz-security-token"] == "session-token"
    assert signed.headers["x-amz-date"]
