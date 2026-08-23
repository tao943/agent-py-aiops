from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import cast

import pytest

from super_ai.chat.query_rewrite import (
    AdaptiveKnowledgeQueryTransformer,
    AdaptiveQueryRewriteRouter,
    QueryRewriteContextMessage,
    StructuredQueryRewriter,
)
from super_ai.retrieval import KnowledgeRetrievalToolInput


@dataclass(slots=True)
class _Response:
    content: object


class _Model:
    def __init__(self, response: object, *, delay_seconds: float = 0.0) -> None:
        self.response = response
        self.delay_seconds = delay_seconds
        self.calls = 0
        self.inputs: list[object] = []

    async def ainvoke(self, input: object) -> object:
        self.calls += 1
        self.inputs.append(input)
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if isinstance(self.response, Exception):
            raise self.response
        return _Response(self.response)


def _redis_context() -> tuple[QueryRewriteContextMessage, ...]:
    return (
        QueryRewriteContextMessage("user", "Redis maxclients 达到上限会有什么表现？"),
        QueryRewriteContextMessage("assistant", "常见表现是新连接被拒绝。"),
    )


def test_standalone_technical_query_is_direct() -> None:
    decision = AdaptiveQueryRewriteRouter().decide(
        "PostgreSQL SQLSTATE 40P01 如何排查", context=()
    )

    assert decision.action == "direct"
    assert decision.reason == "standalone_query"


@pytest.mark.parametrize(
    "query",
    [
        "那怎么办",
        "还有吗",
        "为什么会这样",
        "具体呢",
        "那要先收集什么",
        "那为什么还没恢复",
        "那先检查什么",
        "那应该收集什么",
    ],
)
def test_contextual_follow_up_is_rewritten(query: str) -> None:
    decision = AdaptiveQueryRewriteRouter().decide(query, context=_redis_context())

    assert decision.action == "rewrite"
    assert decision.reason in {"context_reference", "follow_up_expression", "low_information"}


def test_context_reference_without_history_does_not_guess() -> None:
    decision = AdaptiveQueryRewriteRouter().decide("这个怎么处理", context=())

    assert decision.action == "direct_without_context"
    assert decision.reason == "missing_context"


def test_low_information_query_with_technical_token_stays_direct() -> None:
    decision = AdaptiveQueryRewriteRouter().decide("40P01 怎么办", context=_redis_context())

    assert decision.action == "direct"
    assert decision.reason == "standalone_query"


@pytest.mark.asyncio
async def test_structured_rewriter_preserves_history_topic() -> None:
    model = _Model('{"rewrittenQuery":"Redis maxclients 达到上限后的排查步骤","usedContext":true}')
    rewriter = StructuredQueryRewriter(model)

    outcome = await rewriter.rewrite("那这个怎么办", context=_redis_context())

    assert outcome.query == "Redis maxclients 达到上限后的排查步骤"
    assert outcome.audit.applied is True
    assert outcome.audit.model_call_count == 1
    assert outcome.audit.safe_error_code is None
    assert model.calls == 1
    assert "rawResponse" not in outcome.audit.to_safe_metadata()
    assert "prompt" not in outcome.audit.to_safe_metadata()
    assert "reasoning" not in outcome.audit.to_safe_metadata()


@pytest.mark.asyncio
async def test_rewriter_uses_provider_structured_output_contract() -> None:
    class Invoker:
        async def ainvoke(self, input: object) -> object:
            del input
            return {
                "parsed": {
                    "rewrittenQuery": "Redis maxclients 达到上限后的排查步骤",
                    "usedContext": True,
                },
                "parsing_error": None,
            }

    class StructuredModel(_Model):
        def __init__(self) -> None:
            super().__init__(RuntimeError("raw path must not run"))
            self.wrapper_calls = 0

        def with_structured_output(
            self, schema: type[object], **kwargs: object
        ) -> Invoker:
            self.wrapper_calls += 1
            assert schema.__name__ == "_StructuredRewrite"
            assert kwargs == {"method": "json_mode", "include_raw": True}
            return Invoker()

    model = StructuredModel()

    outcome = await StructuredQueryRewriter(
        model, structured_output_method="json_mode"
    ).rewrite("那这个怎么办", context=_redis_context())

    assert outcome.audit.applied is True
    assert outcome.audit.model_call_count == 1
    assert model.wrapper_calls == 1
    assert model.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected_code"),
    [
        ("not json", "rewrite_schema_invalid"),
        ('{"rewrittenQuery":"Redis 排查","usedContext":true,"extra":1}', "rewrite_schema_invalid"),
        ('{"rewrittenQuery":"","usedContext":true}', "rewrite_schema_invalid"),
        (
            '{"rewrittenQuery":"' + ("R" * 513) + '","usedContext":true}',
            "rewrite_schema_invalid",
        ),
        ('{"rewrittenQuery":"Redis 排查","usedContext":false}', "rewrite_schema_invalid"),
        (RuntimeError("provider secret response"), "rewrite_model_failed"),
    ],
)
async def test_rewriter_failures_fall_back_without_leaking(
    response: object, expected_code: str
) -> None:
    model = _Model(response)

    outcome = await StructuredQueryRewriter(model).rewrite(
        "那这个怎么办", context=_redis_context()
    )

    assert outcome.query == "那这个怎么办"
    assert outcome.audit.applied is False
    assert outcome.audit.safe_error_code == expected_code
    assert outcome.audit.model_call_count == 1
    assert "secret" not in str(outcome.audit.to_safe_metadata())


@pytest.mark.asyncio
async def test_rewriter_timeout_falls_back() -> None:
    model = _Model(
        '{"rewrittenQuery":"Redis maxclients 排查","usedContext":true}',
        delay_seconds=0.05,
    )

    outcome = await StructuredQueryRewriter(model, timeout_seconds=0.001).rewrite(
        "那这个怎么办", context=_redis_context()
    )

    assert outcome.query == "那这个怎么办"
    assert outcome.audit.safe_error_code == "rewrite_timeout"
    assert model.calls == 1


@pytest.mark.asyncio
async def test_rewriter_default_timeout_is_twenty_five_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[float | None] = []

    async def capture_timeout(
        awaitable: Awaitable[object], *, timeout: float | None
    ) -> object:
        captured.append(timeout)
        return await awaitable

    monkeypatch.setattr("super_ai.chat.query_rewrite.asyncio.wait_for", capture_timeout)
    model = _Model(
        '{"rewrittenQuery":"Redis maxclients 达到上限后的排查步骤","usedContext":true}'
    )

    outcome = await StructuredQueryRewriter(model).rewrite(
        "那这个怎么办", context=_redis_context()
    )

    assert outcome.audit.applied is True
    assert captured == [25.0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "response"),
    [
        (
            "那这个 order-service 的 40P01 不要重启呢",
            '{"rewrittenQuery":"order-service 的 40P01 重启步骤","usedContext":true}',
        ),
        (
            "那这个资源 pod/api-7f9 未就绪怎么办",
            '{"rewrittenQuery":"pod 未就绪排查","usedContext":true}',
        ),
    ],
)
async def test_rewriter_rejects_lost_identifier_or_negation(
    query: str, response: str
) -> None:
    context = (
        QueryRewriteContextMessage("user", "order-service PostgreSQL 死锁问题"),
        QueryRewriteContextMessage("assistant", "需要先确认等待环。"),
    )

    outcome = await StructuredQueryRewriter(_Model(response)).rewrite(
        query, context=context
    )

    assert outcome.query == query
    assert outcome.audit.safe_error_code == "rewrite_semantic_guard_failed"


@pytest.mark.asyncio
async def test_rewriter_rejects_cross_topic_rewrite() -> None:
    model = _Model(
        '{"rewrittenQuery":"PostgreSQL 连接池耗尽排查","usedContext":true}'
    )

    outcome = await StructuredQueryRewriter(model).rewrite(
        "那这个怎么办", context=_redis_context()
    )

    assert outcome.query == "那这个怎么办"
    assert outcome.audit.safe_error_code == "rewrite_semantic_guard_failed"


@pytest.mark.asyncio
async def test_ambiguous_recent_context_does_not_call_model() -> None:
    context = (
        QueryRewriteContextMessage("user", "比较 Redis 和 PostgreSQL 的超时处理"),
        QueryRewriteContextMessage("assistant", "两者需要不同证据。"),
    )
    model = _Model('{"rewrittenQuery":"Redis 超时排查","usedContext":true}')

    outcome = await StructuredQueryRewriter(model).rewrite("那怎么办", context=context)

    assert outcome.query == "那怎么办"
    assert outcome.audit.action == "direct_without_context"
    assert outcome.audit.safe_error_code == "missing_context"
    assert model.calls == 0


@pytest.mark.asyncio
async def test_request_transformer_spends_rewrite_allowance_only_once() -> None:
    model = _Model(
        '{"rewrittenQuery":"Redis maxclients 达到上限后的排查步骤","usedContext":true}',
        delay_seconds=0.01,
    )
    transformer = AdaptiveKnowledgeQueryTransformer(
        StructuredQueryRewriter(model), context=_redis_context()
    )

    first, second = await asyncio.gather(
        transformer.transform(KnowledgeRetrievalToolInput(query="那这个怎么办")),
        transformer.transform(KnowledgeRetrievalToolInput(query="为什么会这样")),
    )

    assert model.calls == 1
    call_counts = [
        cast(int, first.metadata["modelCallCount"]),
        cast(int, second.metadata["modelCallCount"]),
    ]
    assert sorted(call_counts) == [0, 1]
    assert sorted([first.input.query, second.input.query]) == [
        "Redis maxclients 达到上限后的排查步骤",
        "为什么会这样",
    ]
