from __future__ import annotations

from pathlib import Path

from agents import (
    Agent,
    RunContextWrapper,
    set_default_openai_api,
    set_default_openai_client,
    set_tracing_disabled,
)
from agents.agent import ToolsToFinalOutputResult
from agents.tool import FunctionToolResult
from openai import AsyncOpenAI
from openai.providers import bedrock

from slack_codex.models import InvocationContext
from slack_codex.settings import Settings
from slack_codex.tools import ALL_TOOLS


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


def build_agent(settings: Settings) -> tuple[AsyncOpenAI, Agent]:
    client = AsyncOpenAI(provider=bedrock(region=settings.bedrock_region))
    set_default_openai_client(client, use_for_tracing=False)
    set_default_openai_api("responses")
    set_tracing_disabled(True)
    agent = Agent(
        name="Codex",
        model=settings.model_id,
        instructions=load_instructions(),
        tools=ALL_TOOLS,
        tool_use_behavior=slack_tool_result_behavior,
    )
    return client, agent
