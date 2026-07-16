from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agents import RunContextWrapper

import slack_codex.agent as agent_module
from slack_codex.settings import Settings


def test_agent_instructions_require_slack_mrkdwn_for_all_visible_messages() -> None:
    instructions = agent_module.load_instructions()

    assert "Every Slack-visible message" in instructions
    assert "must use Slack mrkdwn" in instructions
    assert "do not use standard Markdown link syntax" in instructions


def test_build_agent_registers_bedrock_client_before_agent(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, Any]] = []
    provider = object()
    client = object()
    built_agent = object()

    monkeypatch.setattr(
        agent_module,
        "bedrock",
        lambda **kwargs: calls.append(("bedrock", kwargs)) or provider,
    )
    monkeypatch.setattr(
        agent_module,
        "AsyncOpenAI",
        lambda **kwargs: calls.append(("client", kwargs)) or client,
    )
    monkeypatch.setattr(
        agent_module,
        "set_default_openai_client",
        lambda value, **kwargs: calls.append(
            ("default_client", (value, kwargs))
        ),
    )
    monkeypatch.setattr(
        agent_module,
        "set_default_openai_api",
        lambda value: calls.append(("default_api", value)),
    )
    monkeypatch.setattr(
        agent_module,
        "set_tracing_disabled",
        lambda value: calls.append(("tracing_disabled", value)),
    )
    monkeypatch.setattr(
        agent_module,
        "Agent",
        lambda **kwargs: calls.append(("agent", kwargs)) or built_agent,
    )

    settings = Settings(
        aws_region="us-east-1",
        bedrock_region="us-east-1",
        model_id="openai.gpt-5.6-terra",
        slack_bot_token_secret_arn="slack",
        github_app_credentials_secret_arn="github",
        github_repository="owner/repository",
        workspace=tmp_path,
    )

    result_client, result_agent = agent_module.build_agent(settings)

    assert result_client is client
    assert result_agent is built_agent
    assert calls[0] == ("bedrock", {"region": "us-east-1"})
    assert calls[1] == ("client", {"provider": provider})
    assert calls[2] == (
        "default_client",
        (client, {"use_for_tracing": False}),
    )
    assert calls[3:5] == [
        ("default_api", "responses"),
        ("tracing_disabled", True),
    ]
    agent_config = calls[5][1]
    assert agent_config["model"] == "openai.gpt-5.6-terra"
    assert agent_config["tool_use_behavior"] is agent_module.slack_tool_result_behavior


def test_terminal_slack_status_ends_the_agent_turn() -> None:
    context = RunContextWrapper(context=object())
    working = SimpleNamespace(
        tool=SimpleNamespace(name="set_thread_status"),
        output={"success": True, "status": "working"},
    )
    done = SimpleNamespace(
        tool=SimpleNamespace(name="set_thread_status"),
        output={"success": True, "status": "done"},
    )
    question = SimpleNamespace(
        tool=SimpleNamespace(name="ask_user"),
        output={"success": True},
    )

    assert agent_module.slack_tool_result_behavior(context, [working]).is_final_output is False
    assert agent_module.slack_tool_result_behavior(context, [done]).is_final_output is True
    assert agent_module.slack_tool_result_behavior(context, [question]).is_final_output is True
