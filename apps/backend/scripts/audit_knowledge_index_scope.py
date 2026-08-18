"""Audit one PostgreSQL and Milvus knowledge scope without reading chunk content."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from typing import TYPE_CHECKING, Protocol

from super_ai.evaluation.knowledge_scope_audit import audit_knowledge_scope
from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.sqlalchemy import create_sqlalchemy_memory_repositories
from super_ai.vector_store import build_default_milvus_vector_store

if TYPE_CHECKING:
    from collections.abc import Sequence

    from super_ai.evaluation.knowledge_scope_audit import KnowledgeScopeAudit
    from super_ai.memory.repositories import KnowledgeDocumentRecord
    from super_ai.vector_store import StoredVectorChunk


class DocumentLister(Protocol):
    async def list_documents(
        self,
        *,
        owner_user_id: str,
        knowledge_base_id: str,
        include_deleted: bool = False,
    ) -> list[KnowledgeDocumentRecord]: ...


class ChunkLister(Protocol):
    def list_chunks(
        self,
        *,
        tenant_id: str,
        knowledge_base_ids: Sequence[str],
    ) -> list[StoredVectorChunk]: ...


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--knowledge-base-id", required=True)
    parser.add_argument("--config", required=True)
    return parser


async def audit_scope(
    *,
    documents: DocumentLister,
    vector_store: ChunkLister,
    owner_user_id: str,
    knowledge_base_id: str,
) -> KnowledgeScopeAudit:
    scoped_documents = await documents.list_documents(
        owner_user_id=owner_user_id,
        knowledge_base_id=knowledge_base_id,
    )
    scoped_chunks = vector_store.list_chunks(
        tenant_id=owner_user_id,
        knowledge_base_ids=(knowledge_base_id,),
    )
    return audit_knowledge_scope(
        documents=scoped_documents,
        chunks=scoped_chunks,
        owner_user_id=owner_user_id,
        knowledge_base_id=knowledge_base_id,
    )


async def _main() -> int:
    arguments = build_parser().parse_args()
    engine = create_memory_engine(config_path=arguments.config)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        result = await audit_scope(
            documents=repositories.documents,
            vector_store=build_default_milvus_vector_store(config_path=arguments.config),
            owner_user_id=arguments.owner_user_id,
            knowledge_base_id=arguments.knowledge_base_id,
        )
    finally:
        await engine.dispose()
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
