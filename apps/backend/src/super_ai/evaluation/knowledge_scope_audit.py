"""Read-only consistency audit for a benchmark knowledge scope."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from super_ai.memory.repositories import KnowledgeDocumentRecord
    from super_ai.vector_store import StoredVectorChunk


@dataclass(frozen=True, slots=True)
class KnowledgeScopeAudit:
    document_count: int
    chunk_count: int
    missing_document_ids: tuple[str, ...]
    orphan_document_ids: tuple[str, ...]
    document_scope_mismatch_count: int
    chunk_scope_mismatch_count: int
    duplicate_filename_count: int
    passed: bool


def audit_knowledge_scope(
    *,
    documents: Sequence[KnowledgeDocumentRecord],
    chunks: Sequence[StoredVectorChunk],
    owner_user_id: str,
    knowledge_base_id: str,
) -> KnowledgeScopeAudit:
    ready_indexed = tuple(
        document
        for document in documents
        if document.status == "ready"
        and document.index_status == "indexed"
        and document.deleted_at is None
    )
    document_scope_mismatch_count = sum(
        1
        for document in ready_indexed
        if document.owner_user_id != owner_user_id
        or document.knowledge_base_id != knowledge_base_id
    )
    scoped_documents = tuple(
        document
        for document in ready_indexed
        if document.owner_user_id == owner_user_id
        and document.knowledge_base_id == knowledge_base_id
    )
    expected_document_ids = {document.id for document in scoped_documents}
    actual_document_ids = {chunk.document_id for chunk in chunks}
    missing_document_ids = tuple(sorted(expected_document_ids - actual_document_ids))
    orphan_document_ids = tuple(sorted(actual_document_ids - expected_document_ids))
    chunk_scope_mismatch_count = sum(
        1
        for chunk in chunks
        if chunk.owner_user_id != owner_user_id
        or chunk.tenant_id != owner_user_id
        or chunk.knowledge_base_id != knowledge_base_id
    )
    normalized_filenames = [document.filename.casefold() for document in scoped_documents]
    duplicate_filename_count = len(normalized_filenames) - len(set(normalized_filenames))
    passed = (
        len(scoped_documents) == 30
        and not missing_document_ids
        and not orphan_document_ids
        and document_scope_mismatch_count == 0
        and chunk_scope_mismatch_count == 0
        and duplicate_filename_count == 0
    )
    return KnowledgeScopeAudit(
        document_count=len(scoped_documents),
        chunk_count=len(chunks),
        missing_document_ids=missing_document_ids,
        orphan_document_ids=orphan_document_ids,
        document_scope_mismatch_count=document_scope_mismatch_count,
        chunk_scope_mismatch_count=chunk_scope_mismatch_count,
        duplicate_filename_count=duplicate_filename_count,
        passed=passed,
    )
