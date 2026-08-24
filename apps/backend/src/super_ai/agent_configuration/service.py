"""Application service enforcing Agent configuration lifecycle and binding rules."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import cast
from uuid import uuid4

from super_ai.agent_configuration.domain import (
    AGENT_NODES,
    AgentAuditEventRecord,
    AgentBindingRecord,
    AgentNode,
    AgentResourceRecord,
    AgentVersionRecord,
    ConfigurationNotFound,
    InvalidBinding,
    InvalidConfiguration,
    PublishedVersionImmutable,
    ResourceKind,
)
from super_ai.agent_configuration.repositories import AgentConfigurationRepository

_MAX_CONTENT_LENGTH = 65_536
_MAX_NAME_LENGTH = 160
_MAX_DESCRIPTION_LENGTH = 1_024


class AgentConfigurationService:
    """Owner-scoped lifecycle service; authorization is repeated for every mutation."""

    def __init__(self, repository: AgentConfigurationRepository) -> None:
        self._repository = repository

    async def create_resource(
        self,
        *,
        owner_user_id: str,
        actor_user_id: str,
        kind: ResourceKind,
        name: str,
        description: str,
        content: str,
        spec: Mapping[str, object],
    ) -> tuple[AgentResourceRecord, AgentVersionRecord]:
        normalized_name, normalized_description, normalized_content, normalized_spec = (
            _validate_resource(kind, name, description, content, spec)
        )
        return await self._repository.create_resource(
            owner_user_id=owner_user_id,
            actor_user_id=actor_user_id,
            resource_id=f"agent_resource_{uuid4().hex}",
            version_id=f"agent_version_{uuid4().hex}",
            kind=kind,
            name=normalized_name,
            description=normalized_description,
            content=normalized_content,
            spec=normalized_spec,
            content_sha256=_sha256(normalized_content),
        )

    async def get_resource(
        self, *, owner_user_id: str, resource_id: str
    ) -> AgentResourceRecord:
        resource = await self._repository.get_resource(
            owner_user_id=owner_user_id, resource_id=resource_id
        )
        if resource is None:
            raise ConfigurationNotFound("configuration resource not found")
        return resource

    async def list_resources(
        self, *, owner_user_id: str, kind: ResourceKind | None = None
    ) -> list[AgentResourceRecord]:
        return await self._repository.list_resources(owner_user_id=owner_user_id, kind=kind)

    async def list_versions(
        self, *, owner_user_id: str, resource_id: str
    ) -> list[AgentVersionRecord]:
        await self.get_resource(owner_user_id=owner_user_id, resource_id=resource_id)
        return await self._repository.list_versions(
            owner_user_id=owner_user_id, resource_id=resource_id
        )

    async def get_version(
        self, *, owner_user_id: str, version_id: str
    ) -> AgentVersionRecord:
        return await self._require_version(owner_user_id=owner_user_id, version_id=version_id)

    async def create_draft(
        self,
        *,
        owner_user_id: str,
        actor_user_id: str,
        resource_id: str,
        content: str,
        spec: Mapping[str, object],
    ) -> AgentVersionRecord:
        resource = await self.get_resource(owner_user_id=owner_user_id, resource_id=resource_id)
        _, _, normalized_content, normalized_spec = _validate_resource(
            resource.kind, resource.name, resource.description, content, spec
        )
        return await self._repository.create_draft(
            owner_user_id=owner_user_id,
            actor_user_id=actor_user_id,
            resource_id=resource_id,
            version_id=f"agent_version_{uuid4().hex}",
            content=normalized_content,
            spec=normalized_spec,
            content_sha256=_sha256(normalized_content),
        )

    async def update_draft(
        self,
        *,
        owner_user_id: str,
        actor_user_id: str,
        version_id: str,
        content: str,
        spec: Mapping[str, object],
    ) -> AgentVersionRecord:
        current = await self._require_version(owner_user_id=owner_user_id, version_id=version_id)
        if current.status != "draft":
            raise PublishedVersionImmutable("published Agent configuration is immutable")
        resource = await self.get_resource(
            owner_user_id=owner_user_id, resource_id=current.resource_id
        )
        _, _, normalized_content, normalized_spec = _validate_resource(
            resource.kind, resource.name, resource.description, content, spec
        )
        warnings = _validation_warnings(normalized_content)
        updated = await self._repository.update_draft(
            owner_user_id=owner_user_id,
            actor_user_id=actor_user_id,
            version_id=version_id,
            content=normalized_content,
            spec=normalized_spec,
            content_sha256=_sha256(normalized_content),
            validation_warnings=warnings,
        )
        if updated is None:
            raise ConfigurationNotFound("configuration version not found")
        return updated

    async def validate_version(
        self, *, owner_user_id: str, version_id: str
    ) -> tuple[str, ...]:
        version = await self._require_version(
            owner_user_id=owner_user_id, version_id=version_id
        )
        return _validation_warnings(version.content)

    async def publish_version(
        self, *, owner_user_id: str, actor_user_id: str, version_id: str
    ) -> AgentVersionRecord:
        version = await self._repository.publish_version(
            owner_user_id=owner_user_id,
            actor_user_id=actor_user_id,
            version_id=version_id,
        )
        if version is None:
            raise ConfigurationNotFound("configuration version not found")
        return version

    async def deprecate_version(
        self, *, owner_user_id: str, actor_user_id: str, version_id: str
    ) -> AgentVersionRecord:
        version = await self._repository.deprecate_version(
            owner_user_id=owner_user_id,
            actor_user_id=actor_user_id,
            version_id=version_id,
        )
        if version is None:
            raise ConfigurationNotFound("configuration version not found")
        return version

    async def bind(
        self,
        *,
        owner_user_id: str,
        actor_user_id: str,
        node: AgentNode,
        prompt_version_id: str | None,
        skill_version_ids: Sequence[str],
    ) -> AgentBindingRecord:
        if node not in AGENT_NODES:
            raise InvalidBinding("unknown Agent node")
        if prompt_version_id is not None:
            prompt = await self._require_version(
                owner_user_id=owner_user_id, version_id=prompt_version_id
            )
            await self._validate_bound_version(prompt, expected_kind="prompt", node=node)
        unique_skill_ids = tuple(dict.fromkeys(skill_version_ids))
        for skill_version_id in unique_skill_ids:
            skill = await self._require_version(
                owner_user_id=owner_user_id, version_id=skill_version_id
            )
            await self._validate_bound_version(skill, expected_kind="skill", node=node)
        return await self._repository.replace_binding(
            owner_user_id=owner_user_id,
            actor_user_id=actor_user_id,
            binding_id=f"agent_binding_{uuid4().hex}",
            node=node,
            prompt_version_id=prompt_version_id,
            skill_version_ids=unique_skill_ids,
        )

    async def list_bindings(self, *, owner_user_id: str) -> list[AgentBindingRecord]:
        return await self._repository.list_bindings(owner_user_id=owner_user_id)

    async def get_binding(
        self, *, owner_user_id: str, node: AgentNode
    ) -> AgentBindingRecord | None:
        return await self._repository.get_binding(owner_user_id=owner_user_id, node=node)

    async def list_audit_events(
        self, *, owner_user_id: str, limit: int = 100
    ) -> list[AgentAuditEventRecord]:
        return await self._repository.list_audit_events(
            owner_user_id=owner_user_id, limit=max(1, min(limit, 200))
        )

    async def _require_version(
        self, *, owner_user_id: str, version_id: str
    ) -> AgentVersionRecord:
        version = await self._repository.get_version(
            owner_user_id=owner_user_id, version_id=version_id
        )
        if version is None:
            raise ConfigurationNotFound("configuration version not found")
        return version

    async def _validate_bound_version(
        self, version: AgentVersionRecord, *, expected_kind: ResourceKind, node: AgentNode
    ) -> None:
        if version.status != "published":
            raise InvalidBinding("only active published versions can be bound")
        resource = await self.get_resource(
            owner_user_id=version.owner_user_id, resource_id=version.resource_id
        )
        if resource.kind != expected_kind:
            raise InvalidBinding("bound resource kind does not match the binding slot")
        bindable = version.spec.get("bindableNodes", ["conversation"])
        if not isinstance(bindable, list) or node not in bindable:
            raise InvalidBinding("version is not eligible for this Agent node")
        if expected_kind == "skill" and node != "conversation":
            raise InvalidBinding("user Skills may only orchestrate the conversation node")


def _validate_resource(
    kind: ResourceKind,
    name: str,
    description: str,
    content: str,
    spec: Mapping[str, object],
) -> tuple[str, str, str, dict[str, object]]:
    if kind not in {"prompt", "skill"}:
        raise InvalidConfiguration("unsupported resource kind")
    normalized_name = name.strip()
    normalized_description = description.strip()
    normalized_content = content.strip()
    if not normalized_name or not normalized_content:
        raise InvalidConfiguration("name and content are required")
    if len(normalized_name) > _MAX_NAME_LENGTH:
        raise InvalidConfiguration("resource name is too long")
    if len(normalized_description) > _MAX_DESCRIPTION_LENGTH:
        raise InvalidConfiguration("resource description is too long")
    if len(normalized_content) > _MAX_CONTENT_LENGTH:
        raise InvalidConfiguration("resource content is too long")
    normalized_spec = {str(key): value for key, value in spec.items()}
    bindable = normalized_spec.get("bindableNodes", ["conversation"])
    if not isinstance(bindable, list) or not bindable:
        raise InvalidConfiguration("bindableNodes must be a non-empty list")
    bindable_nodes: list[str] = []
    for candidate in cast(list[object], bindable):
        if not isinstance(candidate, str) or candidate not in AGENT_NODES:
            raise InvalidConfiguration("bindableNodes contains an unsupported node")
        bindable_nodes.append(candidate)
    if kind == "skill" and set(bindable_nodes) != {"conversation"}:
        raise InvalidConfiguration("user Skills may bind only to conversation")
    normalized_spec["bindableNodes"] = list(dict.fromkeys(bindable_nodes))
    return normalized_name, normalized_description, normalized_content, normalized_spec


def _validation_warnings(content: str) -> tuple[str, ...]:
    lowered = content.casefold()
    suspicious = ("ignore previous", "绕过", "disable policy", "reveal system prompt")
    return tuple("包含疑似安全边界绕过指令" for phrase in suspicious if phrase in lowered)[:1]


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
