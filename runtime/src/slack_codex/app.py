from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from pydantic import ValidationError

from slack_codex.models import InvocationPayload, TestInvocationPayload
from slack_codex.settings import Settings
from slack_codex.state import RuntimeState

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

_state: RuntimeState | None = None
_background_tasks: set[asyncio.Task[None]] = set()


def get_state() -> RuntimeState:
    if _state is None:
        raise RuntimeError("Runtime state has not been initialized")
    return _state


def set_state_for_testing(state: RuntimeState | None) -> None:
    global _state
    _state = state


@asynccontextmanager
async def lifespan(_app: BedrockAgentCoreApp):
    global _state
    if _state is not None:
        raise RuntimeError("Runtime state was already initialized")

    state = RuntimeState.create(Settings.from_env())
    try:
        await state.start()
    except BaseException:
        await state.close()
        raise

    _state = state
    try:
        yield
    finally:
        _state = None
        await state.close()


app = BedrockAgentCoreApp(lifespan=lifespan)


@app.async_task
async def process_invocation(payload: InvocationPayload) -> None:
    await get_state().run(payload)


def _consume_task_result(task: asyncio.Task[None]) -> None:
    _background_tasks.discard(task)
    try:
        task.result()
    except Exception:
        logger.exception("Agent task failed")


@app.entrypoint
async def invoke(payload: dict[str, Any], context: Any) -> dict[str, Any]:
    if payload.get("source") == "test":
        return await invoke_test(payload, context)

    try:
        parsed = InvocationPayload.model_validate(payload)
    except ValidationError as exc:
        return {"status": "rejected", "error": str(exc)}

    state = get_state()
    try:
        state.bind_session(context.session_id)
    except (ValueError, RuntimeError) as exc:
        return {"status": "rejected", "error": str(exc)}

    if not state.events.add(parsed.event_id):
        return {
            "status": "duplicate",
            "event_id": parsed.event_id,
            "session_id": context.session_id,
        }

    task = asyncio.create_task(process_invocation(parsed))
    _background_tasks.add(task)
    task.add_done_callback(_consume_task_result)
    return {
        "status": "accepted",
        "event_id": parsed.event_id,
        "session_id": context.session_id,
    }


async def invoke_test(payload: dict[str, Any], context: Any) -> dict[str, Any]:
    try:
        parsed = TestInvocationPayload.model_validate(payload)
    except ValidationError as exc:
        return {"status": "rejected", "error": str(exc)}

    state = get_state()
    try:
        state.bind_session(context.session_id)
    except (ValueError, RuntimeError) as exc:
        return {"status": "rejected", "error": str(exc)}

    if not state.events.add(parsed.event_id):
        return {
            "status": "duplicate",
            "event_id": parsed.event_id,
            "session_id": context.session_id,
        }

    result = await state.run_test(parsed)
    result["session_id"] = context.session_id
    return result


def main() -> None:
    app.run()


if __name__ == "__main__":
    main()
