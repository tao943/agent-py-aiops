from __future__ import annotations

import math

import pytest

from super_ai.llm import (
    LlmConfigurationError,
    QwenOpenAIProvider,
    load_llm_provider_config,
)

pytestmark = pytest.mark.live_llm


@pytest.fixture(scope="module")
def live_provider() -> QwenOpenAIProvider:
    try:
        config = load_llm_provider_config()
    except LlmConfigurationError as exc:
        pytest.skip(f"Live LLM configuration is unavailable: {exc}")
    return QwenOpenAIProvider(config)


@pytest.mark.asyncio
async def test_live_chat_readiness(live_provider: QwenOpenAIProvider) -> None:
    result = await live_provider.check_readiness()

    assert result.ok, result.error
    assert result.model == live_provider.config.chat_model


@pytest.mark.asyncio
async def test_live_embedding(live_provider: QwenOpenAIProvider) -> None:
    vectors = await live_provider.create_embedding_model().aembed_documents(
        ["PostgreSQL stores durable Agent state."]
    )

    assert len(vectors) == 1
    assert len(vectors[0]) == live_provider.config.embedding_dimensions
    assert all(math.isfinite(value) for value in vectors[0])


@pytest.mark.asyncio
async def test_live_rerank(live_provider: QwenOpenAIProvider) -> None:
    results = await live_provider.create_rerank_model().arerank(
        query="Which datastore keeps durable Agent state?",
        documents=[
            "PostgreSQL is the durable source of truth.",
            "Redis accelerates event delivery and caching.",
        ],
        top_n=2,
    )

    assert len(results) == 2
    assert {result.index for result in results} == {0, 1}
    assert all(math.isfinite(result.relevance_score) for result in results)
    assert all(0 <= result.relevance_score <= 1 for result in results)
