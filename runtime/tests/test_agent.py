from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from agents import RunContextWrapper

import slack_codex.agent as agent_module
from slack_codex.settings import Settings


def test_agent_instructions_require_slack_mrkdwn_for_all_visible_messages() -> None:
    instructions = agent_module.load_instructions()

    assert "Every Slack-visible message" in instructions
    assert "must use Slack mrkdwn" in instructions
    assert "do not use standard Markdown link syntax" in instructions


def test_agent_instructions_allow_search_and_fetching_search_result_urls() -> None:
    instructions = agent_module.load_instructions()

    assert "`web-search___WebSearch` is a normal research tool" in instructions
    assert "Use it freely whenever" in instructions
    assert "URL returned by search can be passed directly to `fetch_webpage`" in instructions
    assert "199 characters or fewer" in instructions
    assert "1 through 25 results" in instructions
    assert "Treat snippets as\nleads rather than evidence" in instructions


def test_build_agent_registers_bedrock_client_before_agent(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, Any]] = []
    provider = object()
    client = object()
    built_agent = object()
    web_search_server = object()
    web_fetcher = object()

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
        lambda value, **kwargs: calls.append(("default_client", (value, kwargs))),
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
    monkeypatch.setattr(
        agent_module,
        "AgentCoreGatewaySigV4Auth",
        lambda region: calls.append(("gateway_auth", region)) or "gateway-auth",
    )
    monkeypatch.setattr(
        agent_module,
        "MCPServerStreamableHttp",
        lambda params, **kwargs: calls.append(("mcp", (params, kwargs))) or web_search_server,
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

    result_client, result_agent, result_mcp = agent_module.build_agent(  # type: ignore[arg-type]
        settings,
        web_fetcher,
    )

    assert result_client is client
    assert result_agent is built_agent
    assert result_mcp is web_search_server
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
    assert calls[5] == ("gateway_auth", "us-east-1")
    assert calls[6] == (
        "mcp",
        (
            {
                "url": "https://gateway.example/mcp",
                "auth": "gateway-auth",
            },
            {
                "cache_tools_list": True,
                "name": "web-search",
            },
        ),
    )
    agent_config = calls[7][1]
    assert agent_config["model"] == "openai.gpt-5.6-terra"
    assert agent_config["tool_use_behavior"] is agent_module.slack_tool_result_behavior
    assert agent_config["mcp_servers"] == [web_search_server]
    assert any(tool.name == "fetch_webpage" for tool in agent_config["tools"])


def test_build_agent_does_not_duplicate_gateway_mcp_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}
    settings = Settings(
        aws_region="us-east-1",
        bedrock_region="us-east-1",
        model_id="openai.gpt-5.6-terra",
        slack_bot_token_secret_arn="slack",
        github_app_credentials_secret_arn="github",
        github_repository="owner/repository",
        workspace=tmp_path,
        web_search_gateway_url="https://gateway.example/mcp",
    )

    monkeypatch.setattr(agent_module, "AsyncOpenAI", lambda **_kwargs: object())
    monkeypatch.setattr(agent_module, "bedrock", lambda **_kwargs: object())
    monkeypatch.setattr(agent_module, "set_default_openai_client", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(agent_module, "set_default_openai_api", lambda _value: None)
    monkeypatch.setattr(agent_module, "set_tracing_disabled", lambda _value: None)
    monkeypatch.setattr(agent_module, "AgentCoreGatewaySigV4Auth", lambda _region: object())
    monkeypatch.setattr(agent_module, "Agent", lambda **_kwargs: object())
    monkeypatch.setattr(
        agent_module,
        "MCPServerStreamableHttp",
        lambda params, **_kwargs: captured.setdefault("params", params) or object(),
    )

    agent_module.build_agent(settings, object())  # type: ignore[arg-type]

    assert captured["params"]["url"] == "https://gateway.example/mcp"


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


@pytest.mark.parametrize(
    ("prompt", "model_id"),
    [
        ("Please investigate this", "openai.gpt-5.6-luna"),
        ("Please investigate this #terra", "openai.gpt-5.6-terra"),
        ("Please investigate this #sol", "openai.gpt-5.6-sol"),
        ("Please investigate this #TERRA", "openai.gpt-5.6-terra"),
        ("Please investigate #solution", "openai.gpt-5.6-luna"),
    ],
)
def test_model_for_parent_prompt(prompt: str, model_id: str) -> None:
    assert agent_module.model_for_parent_prompt(prompt) == model_id
