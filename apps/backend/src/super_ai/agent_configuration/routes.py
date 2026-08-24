"""Authenticated HTTP API for versioned Agent configuration."""
# pyright: reportUnusedFunction=false

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from super_ai.agent_configuration.domain import (
    AGENT_NODES,
    AgentAuditEventRecord,
    AgentBindingRecord,
    AgentConfigurationError,
    AgentResourceRecord,
    AgentVersionRecord,
    ConfigurationNotFound,
    InvalidBinding,
    InvalidConfiguration,
    PublishedVersionImmutable,
)
from super_ai.agent_configuration.service import AgentConfigurationService
from super_ai.api.responses import ApiErrorException, success_response
from super_ai.auth.repositories import UserRecord


class CreateResourceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["prompt", "skill"]
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=1024)
    content: str = Field(min_length=1, max_length=65_536)
    spec: dict[str, object] = Field(default_factory=dict)


class UpdateDraftBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=1024)
    content: str = Field(min_length=1, max_length=65_536)
    spec: dict[str, object]


class CreateDraftBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=65_536)
    spec: dict[str, object]


class UpdateBindingBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_version_id: str | None = Field(default=None, alias="promptVersionId")
    skill_version_ids: list[str] = Field(default_factory=list, alias="skillVersionIds")


def create_agent_configuration_router(
    *,
    current_user_dependency: Callable[..., Awaitable[UserRecord]],
    service: AgentConfigurationService,
) -> APIRouter:
    router = APIRouter(prefix="/agent-configuration", tags=["agent-configuration"])
    user_dependency = Depends(current_user_dependency)

    @router.get("/resources")
    async def list_resources(request: Request, user: UserRecord = user_dependency) -> object:
        resources = await service.list_resources(owner_user_id=user.id)
        versions: list[AgentVersionRecord] = []
        for resource in resources:
            versions.extend(
                await service.list_versions(owner_user_id=user.id, resource_id=resource.id)
            )
        bindings = await service.list_bindings(owner_user_id=user.id)
        return success_response(
            request,
            {
                "resources": [_resource_payload(item) for item in resources],
                "versions": [_version_payload(item) for item in versions],
                "bindings": [_binding_payload(item) for item in bindings],
                "capabilities": {"canManageConfiguration": True},
            },
        )

    @router.post("/resources")
    async def create_resource(
        request: Request,
        body: CreateResourceBody,
        user: UserRecord = user_dependency,
    ) -> object:
        try:
            resource, version = await service.create_resource(
                owner_user_id=user.id,
                actor_user_id=user.id,
                kind=body.kind,
                name=body.name,
                description=body.description,
                content=body.content,
                spec=body.spec or {"bindableNodes": ["conversation"]},
            )
        except AgentConfigurationError as exc:
            raise _api_error(exc) from exc
        return success_response(
            request, _mutation_payload(resource, version), status_code=201
        )

    @router.post("/resources/{resource_id}/versions")
    async def create_draft(
        request: Request,
        resource_id: str,
        body: CreateDraftBody,
        user: UserRecord = user_dependency,
    ) -> object:
        try:
            resource = await service.get_resource(
                owner_user_id=user.id, resource_id=resource_id
            )
            version = await service.create_draft(
                owner_user_id=user.id,
                actor_user_id=user.id,
                resource_id=resource_id,
                content=body.content,
                spec=body.spec,
            )
        except AgentConfigurationError as exc:
            raise _api_error(exc) from exc
        return success_response(request, _mutation_payload(resource, version), status_code=201)

    @router.put("/versions/{version_id}")
    async def update_draft(
        request: Request,
        version_id: str,
        body: UpdateDraftBody,
        user: UserRecord = user_dependency,
    ) -> object:
        _ = body.name, body.description
        try:
            version = await service.update_draft(
                owner_user_id=user.id,
                actor_user_id=user.id,
                version_id=version_id,
                content=body.content,
                spec=body.spec,
            )
            resource = await service.get_resource(
                owner_user_id=user.id, resource_id=version.resource_id
            )
        except AgentConfigurationError as exc:
            raise _api_error(exc) from exc
        return success_response(request, _mutation_payload(resource, version))

    @router.post("/versions/{version_id}:validate")
    async def validate_version(
        request: Request, version_id: str, user: UserRecord = user_dependency
    ) -> object:
        try:
            warnings = await service.validate_version(
                owner_user_id=user.id, version_id=version_id
            )
        except AgentConfigurationError as exc:
            raise _api_error(exc) from exc
        return success_response(request, {"valid": True, "warnings": list(warnings)})

    @router.post("/versions/{version_id}:publish")
    async def publish_version(
        request: Request, version_id: str, user: UserRecord = user_dependency
    ) -> object:
        return await _lifecycle_mutation(
            request=request,
            user=user,
            version_id=version_id,
            service=service,
            action="publish",
        )

    @router.post("/versions/{version_id}:deprecate")
    async def deprecate_version(
        request: Request, version_id: str, user: UserRecord = user_dependency
    ) -> object:
        return await _lifecycle_mutation(
            request=request,
            user=user,
            version_id=version_id,
            service=service,
            action="deprecate",
        )

    @router.put("/bindings/{node}")
    async def update_binding(
        request: Request,
        node: str,
        body: UpdateBindingBody,
        user: UserRecord = user_dependency,
    ) -> object:
        if node not in AGENT_NODES:
            raise ApiErrorException("VALIDATION_INVALID_ARGUMENT")
        try:
            binding = await service.bind(
                owner_user_id=user.id,
                actor_user_id=user.id,
                node=node,
                prompt_version_id=body.prompt_version_id,
                skill_version_ids=body.skill_version_ids,
            )
        except AgentConfigurationError as exc:
            raise _api_error(exc) from exc
        return success_response(
            request,
            {
                "binding": _binding_payload(binding),
                "capabilities": {"canManageConfiguration": True},
            },
        )

    @router.get("/audit")
    async def list_audit(request: Request, user: UserRecord = user_dependency) -> object:
        events = await service.list_audit_events(owner_user_id=user.id)
        return success_response(
            request,
            {"items": [_audit_payload(item) for item in events], "nextCursor": None},
        )

    return router


async def _lifecycle_mutation(
    *,
    request: Request,
    user: UserRecord,
    version_id: str,
    service: AgentConfigurationService,
    action: Literal["publish", "deprecate"],
) -> object:
    try:
        if action == "publish":
            version = await service.publish_version(
                owner_user_id=user.id, actor_user_id=user.id, version_id=version_id
            )
        else:
            version = await service.deprecate_version(
                owner_user_id=user.id, actor_user_id=user.id, version_id=version_id
            )
        resource = await service.get_resource(
            owner_user_id=user.id, resource_id=version.resource_id
        )
    except AgentConfigurationError as exc:
        raise _api_error(exc) from exc
    return success_response(request, _mutation_payload(resource, version))


def _mutation_payload(
    resource: AgentResourceRecord, version: AgentVersionRecord
) -> dict[str, object]:
    return {
        "resource": _resource_payload(resource),
        "version": _version_payload(version),
        "capabilities": {"canManageConfiguration": True},
    }


def _resource_payload(record: AgentResourceRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "kind": record.kind,
        "name": record.name,
        "description": record.description or None,
        "createdAt": record.created_at.isoformat(),
        "updatedAt": record.updated_at.isoformat(),
    }


def _version_payload(record: AgentVersionRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "resourceId": record.resource_id,
        "version": record.version,
        "status": record.status,
        "content": record.content,
        "spec": record.spec,
        "createdAt": record.created_at.isoformat(),
        "publishedAt": record.published_at.isoformat() if record.published_at else None,
    }


def _binding_payload(record: AgentBindingRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "node": record.node,
        "promptVersionId": record.prompt_version_id,
        "skillVersionIds": list(record.skill_version_ids),
        "updatedAt": record.updated_at.isoformat(),
    }


def _audit_payload(record: AgentAuditEventRecord) -> dict[str, object]:
    action_map = {
        "resource.created": "resource_created",
        "version.draft_created": "draft_saved",
        "version.draft_updated": "draft_saved",
        "version.published": "version_published",
        "version.deprecated": "version_deprecated",
        "binding.updated": "binding_updated",
    }
    return {
        "id": record.event_id,
        "resourceId": record.resource_id,
        "versionId": record.version_id,
        "bindingId": None,
        "action": action_map.get(record.action, "draft_saved"),
        "actorUserId": record.actor_user_id,
        "safeSummary": record.action,
        "createdAt": record.created_at.isoformat(),
    }


def _api_error(exc: AgentConfigurationError) -> ApiErrorException:
    if isinstance(exc, ConfigurationNotFound):
        return ApiErrorException("BUSINESS_NOT_FOUND")
    if isinstance(exc, (PublishedVersionImmutable, InvalidBinding)):
        return ApiErrorException("BUSINESS_CONFLICT")
    if isinstance(exc, InvalidConfiguration):
        return ApiErrorException("VALIDATION_INVALID_ARGUMENT")
    return ApiErrorException("INTERNAL_ERROR")
