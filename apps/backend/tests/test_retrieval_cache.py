from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

import pytest

from super_ai.redis_runtime.cache import CacheLookup
from super_ai.retrieval import (
    KnowledgeRetrievalCitationSource,
    KnowledgeRetrievalError,
    KnowledgeRetrievalHit,
    KnowledgeRetrievalToolInput,
    KnowledgeRetrievalToolResult,
)
from super_ai.retrieval.cached_tool import (
    EMPTY_RETRIEVAL_CACHE_TTL_SECONDS,
    RETRIEVAL_CACHE_TTL_SECONDS,
    CachedKnowledgeRetrievalTool,
)


class FakeCache:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, object]] = {}
        self.ttls: list[int] = []
        self.deleted: list[str] = []
        self.degraded = False

    async def get_json(self, key: str) -> CacheLookup[dict[str, object]]:
        if self.degraded:
            return CacheLookup(state="degraded", value=None)
        value = self.values.get(key)
        return (
            CacheLookup(state="hit", value=value)
            if value is not None
            else CacheLookup(state="miss", value=None)
        )

    async def set_json(self, key: str, value: Mapping[str, object], ttl_seconds: int) -> bool:
        if self.degraded:
            return False
        self.values[key] = dict(value)
        self.ttls.append(ttl_seconds)
        return True

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)
        self.deleted.append(key)


class FakeVersions:
    def __init__(self, version: str = "version-one") -> None:
        self.version = version
        self.raise_error = False
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def get_knowledge_base_cache_version(
        self, *, owner_user_id: str, knowledge_base_ids: Sequence[str]
    ) -> str:
        self.calls.append((owner_user_id, tuple(knowledge_base_ids)))
        if self.raise_error:
            raise RuntimeError("database unavailable")
        return self.version


class CountingRetrievalTool:
    def __init__(self, result: KnowledgeRetrievalToolResult | None = None) -> None:
        self.result = result or _result()
        self.calls = 0
        self.raise_error = False

    async def run(
        self,
        input: KnowledgeRetrievalToolInput,
        *,
        owner_user_id: str,
        accessible_knowledge_base_ids: Sequence[str],
    ) -> KnowledgeRetrievalToolResult:
        self.calls += 1
        if self.raise_error:
            raise KnowledgeRetrievalError(code="SYSTEM_UNAVAILABLE", message="unavailable")
        return self.result


@pytest.mark.asyncio
async def test_cached_retrieval_uses_versioned_owner_isolated_keys_and_ttl() -> None:
    cache = FakeCache()
    versions = FakeVersions()
    inner = CountingRetrievalTool()
    tool = CachedKnowledgeRetrievalTool(inner, cache=cache, versions=versions)
    input = KnowledgeRetrievalToolInput(query="  restart api  ", top_k=2)

    first = await tool.run(
        input, owner_user_id="owner-a", accessible_knowledge_base_ids=["kb-b", "kb-a"]
    )
    second = await tool.run(
        input, owner_user_id="owner-a", accessible_knowledge_base_ids=["kb-a", "kb-b"]
    )
    await tool.run(input, owner_user_id="owner-b", accessible_knowledge_base_ids=["kb-a", "kb-b"])
    versions.version = "version-two"
    await tool.run(input, owner_user_id="owner-a", accessible_knowledge_base_ids=["kb-a", "kb-b"])

    assert first == second
    assert inner.calls == 3
    assert cache.ttls == [RETRIEVAL_CACHE_TTL_SECONDS] * 3
    assert versions.calls[0][1] == ("kb-a", "kb-b")
    assert all("owner-a" not in key and "restart api" not in key for key in cache.values)


@pytest.mark.asyncio
async def test_cached_retrieval_uses_short_ttl_for_empty_results_and_never_caches_errors() -> None:
    cache = FakeCache()
    versions = FakeVersions()
    empty = KnowledgeRetrievalToolResult(query="q", top_k=5, results=[], citations=[])
    inner = CountingRetrievalTool(empty)
    tool = CachedKnowledgeRetrievalTool(inner, cache=cache, versions=versions)
    input = KnowledgeRetrievalToolInput(query="q")

    await tool.run(input, owner_user_id="owner-a", accessible_knowledge_base_ids=["kb-a"])
    inner.raise_error = True
    with pytest.raises(KnowledgeRetrievalError):
        await tool.run(
            KnowledgeRetrievalToolInput(query="error"),
            owner_user_id="owner-a",
            accessible_knowledge_base_ids=["kb-a"],
        )

    assert cache.ttls == [EMPTY_RETRIEVAL_CACHE_TTL_SECONDS]


@pytest.mark.asyncio
async def test_cached_retrieval_bypasses_corrupt_cache_and_version_failures() -> None:
    cache = FakeCache()
    versions = FakeVersions()
    inner = CountingRetrievalTool()
    tool = CachedKnowledgeRetrievalTool(inner, cache=cache, versions=versions)
    input = KnowledgeRetrievalToolInput(query="q")
    await tool.run(input, owner_user_id="owner-a", accessible_knowledge_base_ids=["kb-a"])
    key = next(iter(cache.values))
    cache.values[key] = {"results": "not-a-list"}
    await tool.run(input, owner_user_id="owner-a", accessible_knowledge_base_ids=["kb-a"])
    versions.raise_error = True
    await tool.run(
        KnowledgeRetrievalToolInput(query="new"),
        owner_user_id="owner-a",
        accessible_knowledge_base_ids=["kb-a"],
    )

    assert inner.calls == 3
    assert key in cache.deleted


@pytest.mark.asyncio
async def test_cached_retrieval_rejects_payloads_with_unknown_dto_fields() -> None:
    cache = FakeCache()
    inner = CountingRetrievalTool()
    tool = CachedKnowledgeRetrievalTool(inner, cache=cache, versions=FakeVersions())
    input = KnowledgeRetrievalToolInput(query="q")

    await tool.run(input, owner_user_id="owner-a", accessible_knowledge_base_ids=["kb-a"])
    key = next(iter(cache.values))
    cache.values[key]["unexpected"] = "field"
    await tool.run(input, owner_user_id="owner-a", accessible_knowledge_base_ids=["kb-a"])

    assert inner.calls == 2
    assert key in cache.deleted


@pytest.mark.asyncio
async def test_cached_retrieval_revalidates_owner_and_accessible_knowledge_bases() -> None:
    cache = FakeCache()
    inner = CountingRetrievalTool()
    tool = CachedKnowledgeRetrievalTool(inner, cache=cache, versions=FakeVersions())
    input = KnowledgeRetrievalToolInput(query="q")

    await tool.run(input, owner_user_id="owner-a", accessible_knowledge_base_ids=["kb-a"])
    key = next(iter(cache.values))
    results = cast(list[dict[str, object]], cache.values[key]["results"])
    results[0]["ownerUserId"] = "owner-b"
    await tool.run(input, owner_user_id="owner-a", accessible_knowledge_base_ids=["kb-a"])

    citations = cast(list[dict[str, object]], cache.values[key]["citations"])
    citations[0]["knowledgeBaseId"] = "kb-forbidden"
    await tool.run(input, owner_user_id="owner-a", accessible_knowledge_base_ids=["kb-a"])

    assert inner.calls == 3
    assert cache.deleted == [key, key]


def _result() -> KnowledgeRetrievalToolResult:
    hit = KnowledgeRetrievalHit(
        chunk_id="chunk-a",
        document_id="document-a",
        knowledge_base_id="kb-a",
        owner_user_id="owner-a",
        tenant_id="owner-a",
        content="restart the api",
        source="runbook.md",
        metadata={"service": "api"},
        score=0.9,
    )
    citation = KnowledgeRetrievalCitationSource(
        id="citation-a",
        title="runbook.md",
        source_type="knowledge_document",
        chunk_id=hit.chunk_id,
        document_id=hit.document_id,
        knowledge_base_id=hit.knowledge_base_id,
        source=hit.source,
        metadata=hit.metadata,
        score=hit.score,
        excerpt=hit.content,
        knowledge_type="document",
    )
    return KnowledgeRetrievalToolResult(
        query="restart api", top_k=5, results=[hit], citations=[citation]
    )
