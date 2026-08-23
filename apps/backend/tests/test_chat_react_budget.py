from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import cast

import pytest
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage

from super_ai.chat.execution_policy import ChatExecutionBudget
from super_ai.chat.streaming import (
    ChatExecutionDeadlineExceeded,
    RepeatedToolCallError,
    RepeatedToolCallMiddleware,
    build_agent_middleware,
    chat_execution_error_code,
    iterate_with_deadline,
)


def test_budget_uses_langchain_limit_middleware() -> None:
    middleware = build_agent_middleware(
        ChatExecutionBudget(3, 2, 120.0, max_query_rewrite_calls=1)
    )

    assert isinstance(middleware[0], ModelCallLimitMiddleware)
    assert middleware[0].run_limit == 2
    assert isinstance(middleware[1], ToolCallLimitMiddleware)
    assert isinstance(middleware[2], RepeatedToolCallMiddleware)


@pytest.mark.asyncio
async def test_same_tool_and_arguments_stop_before_second_execution() -> None:
    middleware = RepeatedToolCallMiddleware()
    calls = 0

    async def handler(request: ToolCallRequest) -> ToolMessage:
        nonlocal calls
        calls += 1
        return ToolMessage(content="ok", tool_call_id=request.tool_call["id"])

    request = cast(
        ToolCallRequest,
        SimpleNamespace(
            tool_call={
                "name": "knowledge_retrieval",
                "args": {"query": "locks"},
                "id": "call_1",
                "type": "tool_call",
            }
        ),
    )

    await middleware.awrap_tool_call(request, handler)
    with pytest.raises(RepeatedToolCallError, match="knowledge_retrieval"):
        await middleware.awrap_tool_call(request, handler)

    assert calls == 1


@pytest.mark.asyncio
async def test_deadline_bounds_the_whole_async_iterator() -> None:
    async def events() -> AsyncIterator[int]:
        yield 1
        await asyncio.sleep(60)
        yield 2

    bounded = iterate_with_deadline(events(), 0.01)

    assert await anext(bounded) == 1
    with pytest.raises(ChatExecutionDeadlineExceeded):
        await anext(bounded)


@pytest.mark.parametrize(
    "error",
    [
        RepeatedToolCallError("repeat"),
        ChatExecutionDeadlineExceeded("deadline"),
        type("ModelCallLimitExceededError", (RuntimeError,), {})("model"),
        type("ToolCallLimitExceededError", (RuntimeError,), {})("tool"),
    ],
)
def test_budget_errors_map_to_one_stable_public_code(error: Exception) -> None:
    assert chat_execution_error_code(error) == "CHAT_EXECUTION_BUDGET_EXHAUSTED"
