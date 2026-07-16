from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

from slack_codex.models import TestInvocationPayload
from slack_codex.settings import Settings
from slack_codex.state import RuntimeState


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the AgentCore web tools locally with real AWS credentials and stub Slack."
    )
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--workspace", type=Path, default=Path("/tmp/slack-codex-local"))
    parser.add_argument("--event-id", default=None)
    parser.add_argument(
        "--gateway-url",
        default=os.getenv("WEB_SEARCH_GATEWAY_URL", ""),
        help="Defaults to WEB_SEARCH_GATEWAY_URL.",
    )
    parser.add_argument(
        "--gateway-region",
        default=os.getenv("WEB_SEARCH_GATEWAY_REGION", "us-east-1"),
    )
    parser.add_argument(
        "--model-id",
        default=os.getenv("BEDROCK_MODEL_ID", "openai.gpt-5.6-terra"),
    )
    return parser.parse_args()


def _settings(args: argparse.Namespace) -> Settings:
    gateway_url = args.gateway_url.strip()
    if not gateway_url:
        raise ValueError("--gateway-url or WEB_SEARCH_GATEWAY_URL is required")

    aws_region = os.getenv("AWS_REGION", "us-east-1")
    return Settings(
        aws_region=aws_region,
        bedrock_region=os.getenv("BEDROCK_REGION", aws_region),
        model_id=args.model_id,
        slack_bot_token_secret_arn="local-not-used",
        github_app_credentials_secret_arn="local-not-used",
        github_repository=os.getenv("GH_REPO", "local/local"),
        workspace=args.workspace.resolve(),
        web_search_gateway_url=gateway_url,
        web_search_gateway_region=args.gateway_region,
    )


async def _run(args: argparse.Namespace) -> int:
    state = RuntimeState.create_local(_settings(args))
    try:
        await state.start()
        result = await state.run_test(
            TestInvocationPayload(
                source="test",
                event_id=args.event_id or f"local-{uuid4()}",
                prompt=args.prompt,
            )
        )
        print(json.dumps(result, indent=2, default=str))
        return 0 if result["status"] == "completed" else 1
    finally:
        await state.close()


def main() -> None:
    raise SystemExit(asyncio.run(_run(_arguments())))


if __name__ == "__main__":
    main()
