"""PostgreSQL repository for versioned Agent configuration."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import cast
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from super_ai.agent_configuration.domain import (
    AgentAuditEventRecord,
    AgentBindingRecord,
    AgentNode,
    AgentResourceRecord,
    AgentVersionRecord,
    PublishedVersionImmutable,
    ResourceKind,
    VersionStatus,
)
from super_ai.memory.models import (
    AgentConfigurationAuditEventModel,
    AgentConfigurationBindingModel,
    AgentConfigurationResourceModel,
    AgentConfigurationVersionModel,
    utc_now,
)


class SQLAlchemyAgentConfigurationRepository:
    """Transaction-safe owner-scoped repository."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_resource(
        self,
        *,
        owner_user_id: str,
        actor_user_id: str,
        resource_id: str,
        version_id: str,
        kind: ResourceKind,
        name: str,
        description: str,
        content: str,
        spec: dict[str, object],
        content_sha256: str,
    ) -> tuple[AgentResourceRecord, AgentVersionRecord]:
        now = utc_now()
        resource = AgentConfigurationResourceModel(
            id=resource_id,
            owner_user_id=owner_user_id,
            kind=kind,
            name=name,
            description=description,
            legacy_resource_id=None,
            created_at=now,
            updated_at=now,
        )
        version = AgentConfigurationVersionModel(
            id=version_id,
            resource_id=resource_id,
            owner_user_id=owner_user_id,
            version=1,
            status="draft",
            content=content,
            spec=spec,
            content_sha256=content_sha256,
            validation_warnings=[],
            created_at=now,
            updated_at=now,
            published_at=None,
            deprecated_at=None,
        )
        async with self._session_factory() as session, session.begin():
            session.add_all([resource, version])
            session.add(
                _audit(
                    owner_user_id=owner_user_id,
                    actor_user_id=actor_user_id,
                    action="resource.created",
                    resource_id=resource_id,
                    version_id=version_id,
                    safe_metadata={"kind": kind, "version": 1},
                )
            )
            await session.flush()
            return _resource_record(resource), _version_record(version)

    async def get_resource(
        self, *, owner_user_id: str, resource_id: str
    ) -> AgentResourceRecord | None:
        async with self._session_factory() as session:
            row = (
                await session.scalars(
                    select(AgentConfigurationResourceModel).where(
                        AgentConfigurationResourceModel.id == resource_id,
                        AgentConfigurationResourceModel.owner_user_id == owner_user_id,
                    )
                )
            ).one_or_none()
        return _resource_record(row) if row is not None else None

    async def list_resources(
        self, *, owner_user_id: str, kind: ResourceKind | None = None
    ) -> list[AgentResourceRecord]:
        statement = select(AgentConfigurationResourceModel).where(
            AgentConfigurationResourceModel.owner_user_id == owner_user_id
        )
        if kind is not None:
            statement = statement.where(AgentConfigurationResourceModel.kind == kind)
        statement = statement.order_by(
            AgentConfigurationResourceModel.updated_at.desc(),
            AgentConfigurationResourceModel.id.asc(),
        )
        async with self._session_factory() as session:
            rows = list((await session.scalars(statement)).all())
        return [_resource_record(row) for row in rows]

    async def get_version(
        self, *, owner_user_id: str, version_id: str
    ) -> AgentVersionRecord | None:
        async with self._session_factory() as session:
            row = await _owned_version(session, owner_user_id, version_id)
        return _version_record(row) if row is not None else None

    async def list_versions(
        self, *, owner_user_id: str, resource_id: str
    ) -> list[AgentVersionRecord]:
        statement = (
            select(AgentConfigurationVersionModel)
            .where(
                AgentConfigurationVersionModel.owner_user_id == owner_user_id,
                AgentConfigurationVersionModel.resource_id == resource_id,
            )
            .order_by(AgentConfigurationVersionModel.version.desc())
        )
        async with self._session_factory() as session:
            rows = list((await session.scalars(statement)).all())
        return [_version_record(row) for row in rows]

    async def create_draft(
        self,
        *,
        owner_user_id: str,
        actor_user_id: str,
        resource_id: str,
        version_id: str,
        content: str,
        spec: dict[str, object],
        content_sha256: str,
    ) -> AgentVersionRecord:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            resource = (
                await session.scalars(
                    select(AgentConfigurationResourceModel)
                    .where(
                        AgentConfigurationResourceModel.id == resource_id,
                        AgentConfigurationResourceModel.owner_user_id == owner_user_id,
                    )
                    .with_for_update()
                )
            ).one()
            existing = (
                await session.scalars(
                    select(AgentConfigurationVersionModel).where(
                        AgentConfigurationVersionModel.resource_id == resource_id,
                        AgentConfigurationVersionModel.status == "draft",
                    )
                )
            ).one_or_none()
            if existing is not None:
                return _version_record(existing)
            next_version = int(
                (
                    await session.scalar(
                        select(func.max(AgentConfigurationVersionModel.version)).where(
                            AgentConfigurationVersionModel.resource_id == resource_id
                        )
                    )
                )
                or 0
            ) + 1
            row = AgentConfigurationVersionModel(
                id=version_id,
                resource_id=resource_id,
                owner_user_id=owner_user_id,
                version=next_version,
                status="draft",
                content=content,
                spec=spec,
                content_sha256=content_sha256,
                validation_warnings=[],
                created_at=now,
                updated_at=now,
                published_at=None,
                deprecated_at=None,
            )
            resource.updated_at = now
            session.add(row)
            session.add(
                _audit(
                    owner_user_id=owner_user_id,
                    actor_user_id=actor_user_id,
                    action="version.draft_created",
                    resource_id=resource_id,
                    version_id=version_id,
                    safe_metadata={"version": next_version},
                )
            )
            await session.flush()
            return _version_record(row)

    async def update_draft(
        self,
        *,
        owner_user_id: str,
        actor_user_id: str,
        version_id: str,
        content: str,
        spec: dict[str, object],
        content_sha256: str,
        validation_warnings: Sequence[str],
    ) -> AgentVersionRecord | None:
        async with self._session_factory() as session, session.begin():
            row = await _owned_version(session, owner_user_id, version_id, lock=True)
            if row is None:
                return None
            if row.status != "draft":
                raise PublishedVersionImmutable("published Agent configuration is immutable")
            row.content = content
            row.spec = spec
            row.content_sha256 = content_sha256
            row.validation_warnings = list(validation_warnings)
            row.updated_at = utc_now()
            session.add(
                _audit(
                    owner_user_id=owner_user_id,
                    actor_user_id=actor_user_id,
                    action="version.draft_updated",
                    resource_id=row.resource_id,
                    version_id=row.id,
                    safe_metadata={"warningCount": len(validation_warnings)},
                )
            )
            await session.flush()
            return _version_record(row)

    async def publish_version(
        self, *, owner_user_id: str, actor_user_id: str, version_id: str
    ) -> AgentVersionRecord | None:
        async with self._session_factory() as session, session.begin():
            row = await _owned_version(session, owner_user_id, version_id, lock=True)
            if row is None:
                return None
            if row.status == "published":
                return _version_record(row)
            if row.status != "draft":
                raise PublishedVersionImmutable("deprecated versions cannot be republished")
            now = utc_now()
            row.status = "published"
            row.published_at = now
            row.updated_at = now
            session.add(
                _audit(
                    owner_user_id=owner_user_id,
                    actor_user_id=actor_user_id,
                    action="version.published",
                    resource_id=row.resource_id,
                    version_id=row.id,
                    safe_metadata={"version": row.version},
                )
            )
            await session.flush()
            return _version_record(row)

    async def deprecate_version(
        self, *, owner_user_id: str, actor_user_id: str, version_id: str
    ) -> AgentVersionRecord | None:
        async with self._session_factory() as session, session.begin():
            row = await _owned_version(session, owner_user_id, version_id, lock=True)
            if row is None:
                return None
            if row.status == "deprecated":
                return _version_record(row)
            if row.status != "published":
                raise PublishedVersionImmutable("only published versions can be deprecated")
            now = utc_now()
            row.status = "deprecated"
            row.deprecated_at = now
            row.updated_at = now
            session.add(
                _audit(
                    owner_user_id=owner_user_id,
                    actor_user_id=actor_user_id,
                    action="version.deprecated",
                    resource_id=row.resource_id,
                    version_id=row.id,
                    safe_metadata={"version": row.version},
                )
            )
            await session.flush()
            return _version_record(row)

    async def replace_binding(
        self,
        *,
        owner_user_id: str,
        actor_user_id: str,
        binding_id: str,
        node: AgentNode,
        prompt_version_id: str | None,
        skill_version_ids: Sequence[str],
    ) -> AgentBindingRecord:
        async with self._session_factory() as session, session.begin():
            row = (
                await session.scalars(
                    select(AgentConfigurationBindingModel)
                    .where(
                        AgentConfigurationBindingModel.owner_user_id == owner_user_id,
                        AgentConfigurationBindingModel.node == node,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            now = utc_now()
            if row is None:
                row = AgentConfigurationBindingModel(
                    id=binding_id,
                    owner_user_id=owner_user_id,
                    node=node,
                    prompt_version_id=prompt_version_id,
                    skill_version_ids=list(skill_version_ids),
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                row.prompt_version_id = prompt_version_id
                row.skill_version_ids = list(skill_version_ids)
                row.updated_at = now
            session.add(
                _audit(
                    owner_user_id=owner_user_id,
                    actor_user_id=actor_user_id,
                    action="binding.updated",
                    resource_id=None,
                    version_id=prompt_version_id,
                    node=node,
                    safe_metadata={"skillCount": len(skill_version_ids)},
                )
            )
            await session.flush()
            return _binding_record(row)

    async def get_binding(
        self, *, owner_user_id: str, node: AgentNode
    ) -> AgentBindingRecord | None:
        async with self._session_factory() as session:
            row = (
                await session.scalars(
                    select(AgentConfigurationBindingModel).where(
                        AgentConfigurationBindingModel.owner_user_id == owner_user_id,
                        AgentConfigurationBindingModel.node == node,
                    )
                )
            ).one_or_none()
        return _binding_record(row) if row is not None else None

    async def list_bindings(self, *, owner_user_id: str) -> list[AgentBindingRecord]:
        async with self._session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(AgentConfigurationBindingModel)
                        .where(AgentConfigurationBindingModel.owner_user_id == owner_user_id)
                        .order_by(AgentConfigurationBindingModel.node.asc())
                    )
                ).all()
            )
        return [_binding_record(row) for row in rows]

    async def list_audit_events(
        self, *, owner_user_id: str, limit: int = 100
    ) -> list[AgentAuditEventRecord]:
        async with self._session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(AgentConfigurationAuditEventModel)
                        .where(AgentConfigurationAuditEventModel.owner_user_id == owner_user_id)
                        .order_by(
                            AgentConfigurationAuditEventModel.created_at.desc(),
                            AgentConfigurationAuditEventModel.event_id.desc(),
                        )
                        .limit(limit)
                    )
                ).all()
            )
        return [_audit_record(row) for row in rows]


async def _owned_version(
    session: AsyncSession, owner_user_id: str, version_id: str, *, lock: bool = False
) -> AgentConfigurationVersionModel | None:
    statement = select(AgentConfigurationVersionModel).where(
        AgentConfigurationVersionModel.id == version_id,
        AgentConfigurationVersionModel.owner_user_id == owner_user_id,
    )
    if lock:
        statement = statement.with_for_update()
    return (await session.scalars(statement)).one_or_none()


def _audit(
    *,
    owner_user_id: str,
    actor_user_id: str,
    action: str,
    resource_id: str | None,
    version_id: str | None,
    safe_metadata: dict[str, object],
    node: AgentNode | None = None,
) -> AgentConfigurationAuditEventModel:
    return AgentConfigurationAuditEventModel(
        event_id=f"agent_audit_{uuid4().hex}",
        owner_user_id=owner_user_id,
        actor_user_id=actor_user_id,
        action=action,
        resource_id=resource_id,
        version_id=version_id,
        node=node,
        safe_metadata=safe_metadata,
        created_at=utc_now(),
    )


def _resource_record(row: AgentConfigurationResourceModel) -> AgentResourceRecord:
    return AgentResourceRecord(
        id=row.id,
        owner_user_id=row.owner_user_id,
        kind=cast(ResourceKind, row.kind),
        name=row.name,
        description=row.description,
        legacy_resource_id=row.legacy_resource_id,
        created_at=_utc(row.created_at),
        updated_at=_utc(row.updated_at),
    )


def _version_record(row: AgentConfigurationVersionModel) -> AgentVersionRecord:
    return AgentVersionRecord(
        id=row.id,
        resource_id=row.resource_id,
        owner_user_id=row.owner_user_id,
        version=row.version,
        status=cast(VersionStatus, row.status),
        content=row.content,
        spec=dict(row.spec),
        content_sha256=row.content_sha256,
        validation_warnings=tuple(row.validation_warnings),
        created_at=_utc(row.created_at),
        updated_at=_utc(row.updated_at),
        published_at=_utc(row.published_at) if row.published_at else None,
        deprecated_at=_utc(row.deprecated_at) if row.deprecated_at else None,
    )


def _binding_record(row: AgentConfigurationBindingModel) -> AgentBindingRecord:
    return AgentBindingRecord(
        id=row.id,
        owner_user_id=row.owner_user_id,
        node=cast(AgentNode, row.node),
        prompt_version_id=row.prompt_version_id,
        skill_version_ids=tuple(row.skill_version_ids),
        created_at=_utc(row.created_at),
        updated_at=_utc(row.updated_at),
    )


def _audit_record(row: AgentConfigurationAuditEventModel) -> AgentAuditEventRecord:
    return AgentAuditEventRecord(
        event_id=row.event_id,
        owner_user_id=row.owner_user_id,
        actor_user_id=row.actor_user_id,
        action=row.action,
        resource_id=row.resource_id,
        version_id=row.version_id,
        node=cast(AgentNode | None, row.node),
        safe_metadata=dict(row.safe_metadata),
        created_at=_utc(row.created_at),
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
