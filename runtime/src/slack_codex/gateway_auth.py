from __future__ import annotations

from typing import Protocol

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import ReadOnlyCredentials


class CredentialProvider(Protocol):
    def get_frozen_credentials(self) -> ReadOnlyCredentials: ...


class Session(Protocol):
    def get_credentials(self) -> CredentialProvider | None: ...


class AgentCoreGatewaySigV4Auth(httpx.Auth):
    """Signs each Gateway MCP request with the runtime's rotating IAM credentials."""

    requires_request_body = True

    def __init__(
        self,
        region: str,
        session: Session | None = None,
    ) -> None:
        self._region = region
        self._session = session or boto3.Session()

    def auth_flow(self, request: httpx.Request):
        credentials = self._session.get_credentials()
        if credentials is None:
            raise RuntimeError("No AWS credentials are available to invoke the Gateway")

        aws_request = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content,
            headers=dict(request.headers),
        )
        SigV4Auth(
            credentials.get_frozen_credentials(),
            "bedrock-agentcore",
            self._region,
        ).add_auth(aws_request)
        for name, value in aws_request.headers.items():
            request.headers[name] = value
        yield request
