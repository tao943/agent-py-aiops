"""Owner-scoped cache decorator for validated knowledge retrieval results."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Protocol, cast

from super_ai.redis_runtime.cache import RuntimeCache, build_cache_key
from super_ai.retrieval.tool import (
    KnowledgeRetrievalCitationSource,
    KnowledgeRetrievalHit,
    KnowledgeRetrievalToolInput,
    KnowledgeRetrievalToolResult,
    KnowledgeRetrievalToolRunner,
)

RETRIEVAL_CACHE_TTL_SECONDS = 120
EMPTY_RETRIEVAL_CACHE_TTL_SECONDS = 15


class KnowledgeBaseCacheVersionProvider(Protocol):
    """Minimal PostgreSQL-derived version boundary used by the cache."""

    async def get_knowledge_base_cache_version(
        self,
        *,
        owner_user_id: str,
        knowledge_base_ids: Sequence[str],
    ) -> str:
        """Return a deterministic owner-scoped version."""
        ...


class CachedKnowledgeRetrievalTool:
    """Cache only valid retrieval DTOs; canonical retrieval remains authoritative."""

    name = "knowledge_retrieval"
    description = "Search the current user's indexed knowledge base documents."

    def __init__(
        self,
        inner: KnowledgeRetrievalToolRunner,
        *,
        cache: RuntimeCache | None,
        versions: KnowledgeBaseCacheVersionProvider,
    ) -> None:
        self._inner = inner
        self._cache = cache
        self._versions = versions

    async def run(
        self,
        input: KnowledgeRetrievalToolInput,
        *,
        owner_user_id: str,
        accessible_knowledge_base_ids: Sequence[str],
    ) -> KnowledgeRetrievalToolResult:
        cache = self._cache
        accessible_ids = tuple(sorted(set(accessible_knowledge_base_ids)))
        if cache is None:
            return await self._run_inner(input, owner_user_id, accessible_knowledge_base_ids)
        try:
            version = await self._versions.get_knowledge_base_cache_version(
                owner_user_id=owner_user_id,
                knowledge_base_ids=accessible_ids,
            )
        except Exception:
            return await self._run_inner(input, owner_user_id, accessible_knowledge_base_ids)
        key = build_cache_key(
            purpose="knowledge-retrieval",
            owner_id=owner_user_id,
            version=version,
            input_value=_cache_input(input, accessible_ids),
        )
        try:
            lookup = await cache.get_json(key)
        except Exception:
            return await self._run_inner(input, owner_user_id, accessible_knowledge_base_ids)
        if lookup.state == "hit" and lookup.value is not None:
            cached = _result_from_payload(lookup.value)
            if cached is not None:
                return cached
            try:
                await cache.delete(key)
            except Exception:
                pass
        result = await self._run_inner(input, owner_user_id, accessible_knowledge_base_ids)
        ttl_seconds = (
            EMPTY_RETRIEVAL_CACHE_TTL_SECONDS if not result.results else RETRIEVAL_CACHE_TTL_SECONDS
        )
        try:
            await cache.set_json(key, _result_payload(result), ttl_seconds=ttl_seconds)
        except Exception:
            pass
        return result

    async def _run_inner(
        self,
        input: KnowledgeRetrievalToolInput,
        owner_user_id: str,
        accessible_knowledge_base_ids: Sequence[str],
    ) -> KnowledgeRetrievalToolResult:
        return await self._inner.run(
            input,
            owner_user_id=owner_user_id,
            accessible_knowledge_base_ids=accessible_knowledge_base_ids,
        )


def _cache_input(
    input: KnowledgeRetrievalToolInput, accessible_knowledge_base_ids: Sequence[str]
) -> dict[str, object]:
    filters = input.filters
    return {
        "query": input.query.strip(),
        "topK": input.top_k,
        "accessibleKnowledgeBaseIds": list(accessible_knowledge_base_ids),
        "filters": {
            "knowledgeBaseIds": sorted(set(filters.knowledge_base_ids)) if filters else [],
            "documentIds": sorted(set(filters.document_ids)) if filters else [],
            "metadata": dict(filters.metadata) if filters else {},
        },
    }


def _result_payload(result: KnowledgeRetrievalToolResult) -> dict[str, object]:
    return {
        "query": result.query,
        "topK": result.top_k,
        "results": [_hit_payload(hit) for hit in result.results],
        "citations": [_citation_payload(citation) for citation in result.citations],
    }


def _hit_payload(hit: KnowledgeRetrievalHit) -> dict[str, object]:
    return {
        "chunkId": hit.chunk_id,
        "documentId": hit.document_id,
        "knowledgeBaseId": hit.knowledge_base_id,
        "ownerUserId": hit.owner_user_id,
        "tenantId": hit.tenant_id,
        "content": hit.content,
        "source": hit.source,
        "metadata": dict(hit.metadata),
        "score": hit.score,
        "vectorRank": hit.vector_rank,
        "bm25Rank": hit.bm25_rank,
        "rerankRank": hit.rerank_rank,
        "vectorScore": hit.vector_score,
        "bm25Score": hit.bm25_score,
        "rrfScore": hit.rrf_score,
        "rerankScore": hit.rerank_score,
    }


def _citation_payload(citation: KnowledgeRetrievalCitationSource) -> dict[str, object]:
    return {
        "id": citation.id,
        "title": citation.title,
        "sourceType": citation.source_type,
        "chunkId": citation.chunk_id,
        "documentId": citation.document_id,
        "knowledgeBaseId": citation.knowledge_base_id,
        "source": citation.source,
        "metadata": dict(citation.metadata),
        "score": citation.score,
        "excerpt": citation.excerpt,
        "knowledgeType": citation.knowledge_type,
        "uri": citation.uri,
        "vectorRank": citation.vector_rank,
        "bm25Rank": citation.bm25_rank,
        "rerankRank": citation.rerank_rank,
        "vectorScore": citation.vector_score,
        "bm25Score": citation.bm25_score,
        "rrfScore": citation.rrf_score,
        "rerankScore": citation.rerank_score,
    }


def _result_from_payload(payload: Mapping[str, object]) -> KnowledgeRetrievalToolResult | None:
    if set(payload) != {"query", "topK", "results", "citations"}:
        return None
    query = payload.get("query")
    top_k = payload.get("topK")
    raw_results = payload.get("results")
    raw_citations = payload.get("citations")
    if not isinstance(query, str) or not _valid_positive_int(top_k):
        return None
    if not isinstance(raw_results, list) or not isinstance(raw_citations, list):
        return None
    results = [_hit_from_payload(item) for item in cast(list[object], raw_results)]
    citations = [_citation_from_payload(item) for item in cast(list[object], raw_citations)]
    if any(item is None for item in results) or any(item is None for item in citations):
        return None
    return KnowledgeRetrievalToolResult(
        query=query,
        top_k=cast(int, top_k),
        results=cast(list[KnowledgeRetrievalHit], results),
        citations=cast(list[KnowledgeRetrievalCitationSource], citations),
    )


def _hit_from_payload(value: object) -> KnowledgeRetrievalHit | None:
    value = _object_mapping(value)
    if value is None or set(value) != _HIT_FIELDS:
        return None
    required = (
        "chunkId",
        "documentId",
        "knowledgeBaseId",
        "ownerUserId",
        "tenantId",
        "content",
        "source",
    )
    if not all(isinstance(value.get(key), str) for key in required):
        return None
    metadata = value.get("metadata")
    score = _number(value.get("score"))
    if not isinstance(metadata, Mapping) or score is None:
        return None
    ranks = [_optional_int(value.get(key)) for key in ("vectorRank", "bm25Rank", "rerankRank")]
    scores = [
        _optional_number(value.get(key))
        for key in ("vectorScore", "bm25Score", "rrfScore", "rerankScore")
    ]
    if any(item is _INVALID for item in [*ranks, *scores]):
        return None
    return KnowledgeRetrievalHit(
        chunk_id=cast(str, value["chunkId"]),
        document_id=cast(str, value["documentId"]),
        knowledge_base_id=cast(str, value["knowledgeBaseId"]),
        owner_user_id=cast(str, value["ownerUserId"]),
        tenant_id=cast(str, value["tenantId"]),
        content=cast(str, value["content"]),
        source=cast(str, value["source"]),
        metadata=cast(Mapping[str, object], metadata),
        score=score,
        vector_rank=cast(int | None, ranks[0]),
        bm25_rank=cast(int | None, ranks[1]),
        rerank_rank=cast(int | None, ranks[2]),
        vector_score=cast(float | None, scores[0]),
        bm25_score=cast(float | None, scores[1]),
        rrf_score=cast(float | None, scores[2]),
        rerank_score=cast(float | None, scores[3]),
    )


def _citation_from_payload(value: object) -> KnowledgeRetrievalCitationSource | None:
    value = _object_mapping(value)
    if value is None or set(value) != _CITATION_FIELDS:
        return None
    required = (
        "id",
        "title",
        "sourceType",
        "chunkId",
        "documentId",
        "knowledgeBaseId",
        "source",
        "excerpt",
        "knowledgeType",
    )
    if not all(isinstance(value.get(key), str) for key in required):
        return None
    metadata = value.get("metadata")
    score = _number(value.get("score"))
    uri = value.get("uri")
    if (
        not isinstance(metadata, Mapping)
        or score is None
        or (uri is not None and not isinstance(uri, str))
    ):
        return None
    ranks = [_optional_int(value.get(key)) for key in ("vectorRank", "bm25Rank", "rerankRank")]
    scores = [
        _optional_number(value.get(key))
        for key in ("vectorScore", "bm25Score", "rrfScore", "rerankScore")
    ]
    if any(item is _INVALID for item in [*ranks, *scores]):
        return None
    return KnowledgeRetrievalCitationSource(
        id=cast(str, value["id"]),
        title=cast(str, value["title"]),
        source_type=cast(str, value["sourceType"]),
        chunk_id=cast(str, value["chunkId"]),
        document_id=cast(str, value["documentId"]),
        knowledge_base_id=cast(str, value["knowledgeBaseId"]),
        source=cast(str, value["source"]),
        metadata=cast(Mapping[str, object], metadata),
        score=score,
        excerpt=cast(str, value["excerpt"]),
        knowledge_type=cast(str, value["knowledgeType"]),
        uri=uri,
        vector_rank=cast(int | None, ranks[0]),
        bm25_rank=cast(int | None, ranks[1]),
        rerank_rank=cast(int | None, ranks[2]),
        vector_score=cast(float | None, scores[0]),
        bm25_score=cast(float | None, scores[1]),
        rrf_score=cast(float | None, scores[2]),
        rerank_score=cast(float | None, scores[3]),
    )


_INVALID = object()

_HIT_FIELDS = {
    "chunkId",
    "documentId",
    "knowledgeBaseId",
    "ownerUserId",
    "tenantId",
    "content",
    "source",
    "metadata",
    "score",
    "vectorRank",
    "bm25Rank",
    "rerankRank",
    "vectorScore",
    "bm25Score",
    "rrfScore",
    "rerankScore",
}

_CITATION_FIELDS = {
    "id",
    "title",
    "sourceType",
    "chunkId",
    "documentId",
    "knowledgeBaseId",
    "source",
    "metadata",
    "score",
    "excerpt",
    "knowledgeType",
    "uri",
    "vectorRank",
    "bm25Rank",
    "rerankRank",
    "vectorScore",
    "bm25Score",
    "rrfScore",
    "rerankScore",
}


def _object_mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    mapping = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        return None
    return cast(Mapping[str, object], mapping)


def _valid_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _optional_int(value: object) -> int | None | object:
    return value if _valid_positive_int(value) else None if value is None else _INVALID


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _optional_number(value: object) -> float | None | object:
    return None if value is None else _number(value) if _number(value) is not None else _INVALID
