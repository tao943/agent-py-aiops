"""Owner-scoped HTTP API for governed production recovery intents."""
# pyright: reportUnusedFunction=false

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Annotated, Protocol, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from super_ai.api.responses import ApiErrorException, success_response
from super_ai.auth.repositories import UserRecord
from super_ai.recovery.config import ProductionRecoverySettings
from super_ai.recovery.intent_service import (
    RecoveryIntentNotEligible,
    RecoveryIntentService,
)
from super_ai.recovery.repository import (
    RecoveryIntentRepository,
    RecoveryStateConflict,
)

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
_FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "password",
        "token",
        "dsn",
        "sql",
        "pid",
        "path",
        "stdout",
        "stderr",
        "exception",
        "trustedsnapshot",
        "canonicalarguments",
    }
)


class CurrentUserDependency(Protocol):
    async def __call__(self, request: Request, credentials: object) -> UserRecord: ...


class RuntimeStarter(Protocol):
    async def start(self) -> None: ...


RateLimitGuard = Callable[[str], Awaitable[None]]


class _StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateRecoveryIntentBody(_StrictBody):
    note: str | None = Field(default=None, max_length=1000)


class ApproveRecoveryIntentBody(_StrictBody):
    incident_id_confirmation: str = Field(
        alias="incidentIdConfirmation",
        min_length=1,
        max_length=96,
    )


def create_recovery_router(
    *,
    current_user_dependency: Callable[..., Awaitable[UserRecord]],
    service: RecoveryIntentService,
    intents: RecoveryIntentRepository,
    settings: ProductionRecoverySettings,
    runtime: RuntimeStarter,
    rate_limit_guard: RateLimitGuard,
    now: Callable[[], datetime] | None = None,
) -> APIRouter:
    router = APIRouter()
    clock = now or (lambda: datetime.now(timezone.utc))
    user_dependency = Depends(current_user_dependency)

    @router.post("/aiops/diagnostics/{task_id}/recovery-intents")
    async def create_intent(
        request: Request,
        task_id: str,
        body: CreateRecoveryIntentBody,
        user: UserRecord = user_dependency,
    ) -> object:
        _require_id(task_id)
        try:
            result = await service.create_result(
                owner_user_id=user.id,
                diagnostic_task_id=task_id,
                note=body.note,
            )
        except RecoveryIntentNotEligible as exc:
            raise ApiErrorException("AUTH_FORBIDDEN") from exc
        if result.intent.status == "queued":
            await runtime.start()
        return _safe_success(
            request,
            result.intent.public_payload(),
            status_code=200 if result.reused else 201,
        )

    @router.get("/aiops/recovery-intents/{intent_id}")
    async def get_intent(
        request: Request,
        intent_id: str,
        user: UserRecord = user_dependency,
    ) -> object:
        intent = await _owned_intent(intents, user.id, intent_id)
        return _safe_success(request, intent.public_payload())

    @router.post("/aiops/recovery-intents/{intent_id}:approve")
    async def approve_intent(
        request: Request,
        intent_id: str,
        body: ApproveRecoveryIntentBody,
        user: UserRecord = user_dependency,
    ) -> object:
        intent = await _owned_intent(intents, user.id, intent_id)
        if body.incident_id_confirmation != intent.incident_id:
            raise ApiErrorException("VALIDATION_INVALID_ARGUMENT")
        await rate_limit_guard(user.id)
        if intent.status == "queued":
            await runtime.start()
            return _safe_success(request, intent.public_payload(), status_code=202)
        if intent.status != "awaiting_approval":
            raise ApiErrorException("BUSINESS_CONFLICT")
        current_time = clock()
        try:
            updated = await intents.approve_with_job_and_event(
                owner_user_id=user.id,
                intent_id=intent.id,
                approval_id=_new_id("approval"),
                confirmation_fingerprint=intent.proposal_fingerprint,
                background_job_id=_new_id("job"),
                event_id=_new_id("event"),
                now=current_time,
                expires_at=current_time + timedelta(seconds=settings.approval_ttl_seconds),
            )
        except RecoveryStateConflict as exc:
            raise ApiErrorException("BUSINESS_CONFLICT") from exc
        if updated is None:
            raise ApiErrorException("AUTH_FORBIDDEN")
        await runtime.start()
        return _safe_success(request, updated.public_payload(), status_code=202)

    @router.post("/aiops/recovery-intents/{intent_id}:reject")
    async def reject_intent(
        request: Request,
        intent_id: str,
        user: UserRecord = user_dependency,
    ) -> object:
        intent = await _owned_intent(intents, user.id, intent_id)
        if intent.status == "rejected":
            return _safe_success(request, intent.public_payload())
        try:
            updated = await intents.reject(
                owner_user_id=user.id,
                intent_id=intent.id,
                event_id=_new_id("event"),
                now=clock(),
            )
        except RecoveryStateConflict as exc:
            raise ApiErrorException("BUSINESS_CONFLICT") from exc
        if updated is None:
            raise ApiErrorException("AUTH_FORBIDDEN")
        return _safe_success(request, updated.public_payload())

    @router.post("/aiops/recovery-intents/{intent_id}:cancel")
    async def cancel_intent(
        request: Request,
        intent_id: str,
        user: UserRecord = user_dependency,
    ) -> object:
        intent = await _owned_intent(intents, user.id, intent_id)
        if intent.status == "cancelled":
            return _safe_success(request, intent.public_payload())
        try:
            updated = await intents.cancel_before_claim(
                owner_user_id=user.id,
                intent_id=intent.id,
                event_id=_new_id("event"),
                now=clock(),
            )
        except RecoveryStateConflict as exc:
            raise ApiErrorException("BUSINESS_CONFLICT") from exc
        if updated is None:
            raise ApiErrorException("AUTH_FORBIDDEN")
        return _safe_success(request, updated.public_payload())

    @router.get("/aiops/recovery-intents/{intent_id}/events")
    async def list_events(
        request: Request,
        intent_id: str,
        user: UserRecord = user_dependency,
        after_sequence: Annotated[int, Query(alias="afterSequence", ge=0)] = 0,
    ) -> object:
        intent = await _owned_intent(intents, user.id, intent_id)
        events = await intents.list_events(
            owner_user_id=user.id,
            intent_id=intent.id,
            after_sequence=after_sequence,
        )
        return _safe_success(
            request,
            {"items": [event.public_payload() for event in events]},
        )

    return router


async def _owned_intent(
    repository: RecoveryIntentRepository,
    owner_user_id: str,
    intent_id: str,
):  # type: ignore[no-untyped-def]
    _require_id(intent_id)
    intent = await repository.get_owned(
        owner_user_id=owner_user_id,
        intent_id=intent_id,
    )
    if intent is None:
        raise ApiErrorException("AUTH_FORBIDDEN")
    return intent


def _require_id(value: str) -> None:
    if not _ID.fullmatch(value):
        raise ApiErrorException("VALIDATION_INVALID_ARGUMENT")


def _safe_success(
    request: Request,
    payload: object,
    *,
    status_code: int = 200,
):  # type: ignore[no-untyped-def]
    _assert_public_payload(payload)
    return success_response(request, payload, status_code=status_code)


def _assert_public_payload(value: object) -> None:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        for key, nested in mapping.items():
            if not isinstance(key, str) or key.lower() in _FORBIDDEN_PUBLIC_KEYS:
                raise RuntimeError("unsafe_recovery_public_payload")
            _assert_public_payload(nested)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for nested in cast(Sequence[object], value):
            _assert_public_payload(nested)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"
