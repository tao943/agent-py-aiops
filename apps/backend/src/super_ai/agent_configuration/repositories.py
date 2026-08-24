"""Persistence protocol for versioned Agent configuration."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from super_ai.agent_configuration.domain import (
    AgentAuditEventRecord,
    AgentBindingRecord,
    AgentNode,
    AgentResourceRecord,
    AgentVersionRecord,
    ResourceKind,
)


class AgentConfigurationRepository(Protocol):
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
    ) -> tuple[AgentResourceRecord, AgentVersionRecord]: ...

    async def get_resource(
        self, *, owner_user_id: str, resource_id: str
    ) -> AgentResourceRecord | None: ...

    async def list_resources(
        self, *, owner_user_id: str, kind: ResourceKind | None = None
    ) -> list[AgentResourceRecord]: ...

    async def get_version(
        self, *, owner_user_id: str, version_id: str
    ) -> AgentVersionRecord | None: ...

    async def list_versions(
        self, *, owner_user_id: str, resource_id: str
    ) -> list[AgentVersionRecord]: ...

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
    ) -> AgentVersionRecord: ...

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
    ) -> AgentVersionRecord | None: ...

    async def publish_version(
        self, *, owner_user_id: str, actor_user_id: str, version_id: str
    ) -> AgentVersionRecord | None: ...

    async def deprecate_version(
        self, *, owner_user_id: str, actor_user_id: str, version_id: str
    ) -> AgentVersionRecord | None: ...

    async def replace_binding(
        self,
        *,
        owner_user_id: str,
        actor_user_id: str,
        binding_id: str,
        node: AgentNode,
        prompt_version_id: str | None,
        skill_version_ids: Sequence[str],
    ) -> AgentBindingRecord: ...

    async def get_binding(
        self, *, owner_user_id: str, node: AgentNode
    ) -> AgentBindingRecord | None: ...

    async def list_bindings(self, *, owner_user_id: str) -> list[AgentBindingRecord]: ...

    async def list_audit_events(
        self, *, owner_user_id: str, limit: int = 100
    ) -> list[AgentAuditEventRecord]: ...
