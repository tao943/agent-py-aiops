from __future__ import annotations

import importlib.util
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from super_ai.evaluation.knowledge_scope_audit import audit_knowledge_scope
from super_ai.memory.repositories import KnowledgeDocumentRecord
from super_ai.vector_store import StoredVectorChunk

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit_knowledge_index_scope.py"
SPEC = importlib.util.spec_from_file_location("audit_knowledge_index_scope", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)


OWNER_ID = "benchmark-owner"
KNOWLEDGE_BASE_ID = "benchmark-kb"


def _document(index: int) -> KnowledgeDocumentRecord:
    now = datetime.now(timezone.utc)
    return KnowledgeDocumentRecord(
        id=f"document-{index:02d}",
        owner_user_id=OWNER_ID,
        knowledge_base_id=KNOWLEDGE_BASE_ID,
        filename=f"card-{index:02d}.md",
        size_bytes=100,
        mime_type="text/markdown",
        content_hash=f"hash-{index:02d}",
        status="ready",
        index_status="indexed",
        metadata={},
        uploaded_at=now,
        updated_at=now,
        source="benchmark",
        deleted_at=None,
    )


def _chunk(index: int) -> StoredVectorChunk:
    return StoredVectorChunk(
        chunk_id=f"chunk-{index:02d}",
        document_id=f"document-{index:02d}",
        knowledge_base_id=KNOWLEDGE_BASE_ID,
        owner_user_id=OWNER_ID,
        tenant_id=OWNER_ID,
        content="redacted test content",
        source="benchmark",
        created_at=1,
        metadata={},
    )


def _valid_scope() -> tuple[list[KnowledgeDocumentRecord], list[StoredVectorChunk]]:
    return ([_document(index) for index in range(30)], [_chunk(index) for index in range(30)])


def test_audit_passes_for_exact_thirty_document_scope() -> None:
    documents, chunks = _valid_scope()

    result = audit_knowledge_scope(
        documents=documents,
        chunks=chunks,
        owner_user_id=OWNER_ID,
        knowledge_base_id=KNOWLEDGE_BASE_ID,
    )

    assert result.passed is True
    assert result.document_count == 30
    assert result.chunk_count == 30
    assert result.missing_document_ids == ()
    assert result.orphan_document_ids == ()
    assert result.document_scope_mismatch_count == 0
    assert result.chunk_scope_mismatch_count == 0
    assert result.duplicate_filename_count == 0


def test_audit_rejects_missing_document_count() -> None:
    documents, chunks = _valid_scope()

    result = audit_knowledge_scope(
        documents=documents[:-1],
        chunks=chunks[:-1],
        owner_user_id=OWNER_ID,
        knowledge_base_id=KNOWLEDGE_BASE_ID,
    )

    assert result.passed is False
    assert result.document_count == 29


def test_audit_reports_document_without_chunk() -> None:
    documents, chunks = _valid_scope()

    result = audit_knowledge_scope(
        documents=documents,
        chunks=chunks[:-1],
        owner_user_id=OWNER_ID,
        knowledge_base_id=KNOWLEDGE_BASE_ID,
    )

    assert result.passed is False
    assert result.missing_document_ids == ("document-29",)


def test_audit_reports_orphan_chunk() -> None:
    documents, chunks = _valid_scope()
    chunks.append(replace(chunks[-1], chunk_id="orphan", document_id="not-a-document"))

    result = audit_knowledge_scope(
        documents=documents,
        chunks=chunks,
        owner_user_id=OWNER_ID,
        knowledge_base_id=KNOWLEDGE_BASE_ID,
    )

    assert result.passed is False
    assert result.orphan_document_ids == ("not-a-document",)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("owner_user_id", "wrong-owner"),
        ("tenant_id", "wrong-tenant"),
        ("knowledge_base_id", "wrong-kb"),
    ),
)
def test_audit_rejects_chunk_scope_mismatch(field: str, value: str) -> None:
    documents, chunks = _valid_scope()
    chunks[0] = replace(chunks[0], **{field: value})

    result = audit_knowledge_scope(
        documents=documents,
        chunks=chunks,
        owner_user_id=OWNER_ID,
        knowledge_base_id=KNOWLEDGE_BASE_ID,
    )

    assert result.passed is False
    assert result.chunk_scope_mismatch_count == 1


@pytest.mark.parametrize(
    ("field", "value"),
    (("owner_user_id", "wrong-owner"), ("knowledge_base_id", "wrong-kb")),
)
def test_audit_rejects_document_scope_mismatch(field: str, value: str) -> None:
    documents, chunks = _valid_scope()
    documents[0] = replace(documents[0], **{field: value})

    result = audit_knowledge_scope(
        documents=documents,
        chunks=chunks,
        owner_user_id=OWNER_ID,
        knowledge_base_id=KNOWLEDGE_BASE_ID,
    )

    assert result.passed is False
    assert result.document_scope_mismatch_count == 1


def test_audit_rejects_duplicate_active_filename_case_insensitively() -> None:
    documents, chunks = _valid_scope()
    documents[1] = replace(documents[1], filename=documents[0].filename.upper())

    result = audit_knowledge_scope(
        documents=documents,
        chunks=chunks,
        owner_user_id=OWNER_ID,
        knowledge_base_id=KNOWLEDGE_BASE_ID,
    )

    assert result.passed is False
    assert result.duplicate_filename_count == 1


class FakeDocumentLister:
    async def list_documents(
        self,
        *,
        owner_user_id: str,
        knowledge_base_id: str,
        include_deleted: bool = False,
    ) -> list[KnowledgeDocumentRecord]:
        assert (owner_user_id, knowledge_base_id, include_deleted) == (
            OWNER_ID,
            KNOWLEDGE_BASE_ID,
            False,
        )
        return _valid_scope()[0]


class FakeChunkLister:
    def list_chunks(
        self, *, tenant_id: str, knowledge_base_ids: tuple[str, ...]
    ) -> list[StoredVectorChunk]:
        assert (tenant_id, knowledge_base_ids) == (OWNER_ID, (KNOWLEDGE_BASE_ID,))
        return _valid_scope()[1]


def test_audit_cli_parser_requires_explicit_scope_and_config() -> None:
    with pytest.raises(SystemExit):
        CLI.build_parser().parse_args([])


@pytest.mark.asyncio
async def test_audit_cli_reads_only_the_explicit_scope() -> None:
    result = await CLI.audit_scope(
        documents=FakeDocumentLister(),
        vector_store=FakeChunkLister(),
        owner_user_id=OWNER_ID,
        knowledge_base_id=KNOWLEDGE_BASE_ID,
    )

    assert result.passed is True
