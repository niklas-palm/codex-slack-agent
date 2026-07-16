from __future__ import annotations

import base64
from typing import Any

import boto3


class SecretLoader:
    def __init__(self, region: str, client: Any | None = None) -> None:
        self._client = client or boto3.client("secretsmanager", region_name=region)
        self._cache: dict[str, str] = {}

    def get(self, secret_arn: str) -> str:
        if secret_arn in self._cache:
            return self._cache[secret_arn]

        response = self._client.get_secret_value(SecretId=secret_arn)
        value = response.get("SecretString")
        if value is None and response.get("SecretBinary") is not None:
            value = base64.b64decode(response["SecretBinary"]).decode("utf-8")
        if not value:
            raise RuntimeError(f"Secret has no value: {secret_arn}")

        self._cache[secret_arn] = value
        return value
