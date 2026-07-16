from __future__ import annotations

import re
from pathlib import Path

from agents import (
    Agent,
    RunContextWrapper,
    set_default_openai_api,
    set_default_openai_client,
    set_tracing_disabled,
)
from agents.agent import ToolsToFinalOutputResult
from agents.mcp import MCPServerStreamableHttp
from agents.tool import FunctionToolResult
from openai import AsyncOpenAI
from openai.providers import bedrock

from slack_codex.gateway_auth import AgentCoreGatewaySigV4Auth
from slack_codex.models import InvocationContext
from slack_codex.settings import Settings
from slack_codex.tools import ALL_TOOLS
from slack_codex.web_fetch import WebFetcher, build_web_tools

MODEL_IDS = {
    "luna": "openai.gpt-5.6-luna",
    "terra": "openai.gpt-5.6-terra",
    "sol": "openai.gpt-5.6-sol",
}
_MODEL_DIRECTIVE = re.compile(r"(?<!\w)#(terra|sol)\b", re.IGNORECASE)


def model_for_parent_prompt(prompt: str) -> str:
    """Choose a model from an optional parent-message directive."""
    match = _MODEL_DIRECTIVE.search(prompt)
    return MODEL_IDS[match.group(1).lower()] if match else MODEL_IDS["luna"]


def load_instructions() -> str:
    return Path(__file__).with_name("prompt.md").read_text(encoding="utf-8")


def slack_tool_result_behavior(
    _context: RunContextWrapper[InvocationContext],
    tool_results: list[FunctionToolResult],
) -> ToolsToFinalOutputResult:
    for result in tool_results:
        if result.tool.name == "ask_user":
            return ToolsToFinalOutputResult(
                is_final_output=True,
                final_output=result.output,
            )
        if (
            result.tool.name == "set_thread_status"
            and isinstance(result.output, dict)
            and result.output.get("success") is True
            and result.output.get("status") in {"done", "failed"}
        ):
            return ToolsToFinalOutputResult(
                is_final_output=True,
                final_output=result.output,
            )
    return ToolsToFinalOutputResult(is_final_output=False)


def build_agent(
    settings: Settings,
    web_fetcher: WebFetcher,
) -> tuple[AsyncOpenAI, Agent, MCPServerStreamableHttp]:
    client = AsyncOpenAI(provider=bedrock(region=settings.bedrock_region))
    set_default_openai_client(client, use_for_tracing=False)
    set_default_openai_api("responses")
    set_tracing_disabled(True)
    gateway_url = settings.web_search_gateway_url.rstrip("/")
    if not gateway_url.endswith("/mcp"):
        gateway_url = f"{gateway_url}/mcp"
    web_search_server = MCPServerStreamableHttp(
        {
            "url": gateway_url,
            "auth": AgentCoreGatewaySigV4Auth(settings.web_search_gateway_region),
        },
        cache_tools_list=True,
        name="web-search",
    )
    agent = Agent(
        name="Codex",
        model=settings.model_id,
        instructions=load_instructions(),
        tools=[*ALL_TOOLS, *build_web_tools(web_fetcher)],
        mcp_servers=[web_search_server],
        tool_use_behavior=slack_tool_result_behavior,
    )
    return client, agent, web_search_server
