from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


@dataclass(frozen=True)
class Settings:
    aws_region: str
    bedrock_region: str
    model_id: str
    slack_bot_token_secret_arn: str
    github_app_credentials_secret_arn: str
    github_repository: str
    workspace: Path
    web_search_gateway_url: str = "https://gateway.example"
    web_search_gateway_region: str = "us-east-1"

    @classmethod
    def from_env(cls) -> Settings:
        aws_region = os.getenv("AWS_REGION", "us-east-1")
        return cls(
            aws_region=aws_region,
            bedrock_region=os.getenv("BEDROCK_REGION", aws_region),
            model_id=os.getenv("BEDROCK_MODEL_ID", "openai.gpt-5.6-luna"),
            slack_bot_token_secret_arn=_required("SLACK_BOT_TOKEN_SECRET_ARN"),
            github_app_credentials_secret_arn=_required(
                "GITHUB_APP_CREDENTIALS_SECRET_ARN"
            ),
            github_repository=_required("GH_REPO"),
            workspace=Path(os.getenv("WORKSPACE_DIR", "/workspace")).resolve(),
            web_search_gateway_url=_required("WEB_SEARCH_GATEWAY_URL"),
            web_search_gateway_region=os.getenv(
                "WEB_SEARCH_GATEWAY_REGION",
                "us-east-1",
            ),
        )
