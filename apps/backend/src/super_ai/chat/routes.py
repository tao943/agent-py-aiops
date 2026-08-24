"""Owner-scoped HTTP routes for durable Conversation Agent runs."""
# pyright: reportUnusedFunction=false

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from hashlib import sha256

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from super_ai.agent_configuration.runtime import AgentConfigurationRuntime
from super_ai.auth.repositories import UserRecord
from super_ai.chat.pending_actions import PendingActionNotFound, PendingChatActionService
from super_ai.chat.run_events import encode_run_sse, public_run_event
from super_ai.chat.streaming import sanitize_chat_metadata
from super_ai.memory.repositories import ChatRunRecord, MemoryRepositories

_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
_PUBLIC_EVENT_TYPES = frozenset(
    {
        "run.status",
        "content.delta",
        "tool.call",
        "reference.source",
        "diagnostic.result",
        "execution.mode_selected",
        "structured.result",
        "confirmation.required",
        "confirmation.resolved",
        "explanation.delta",
        "explanation.degraded",
        "budget.exhausted",
        "run.restarted",
        "complete",
        "error",
    }
)


class CreateChatRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    content: str = Field(min_length=1, max_length=200_000)
    metadata: dict[str, object] = Field(default_factory=dict)
    client_request_id: str = Field(alias="clientRequestId", min_length=1, max_length=120)


def create_chat_runs_router(
    *,
    repositories: MemoryRepositories,
    current_user_dependency: Callable[..., object],
    agent_configuration_runtime: AgentConfigurationRuntime | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/chat/sessions/{session_id}/runs", tags=["chat"])

    @router.post("")
    async def create_run(
        request: Request,
        session_id: str,
        body: CreateChatRunRequest,
        user: UserRecord = Depends(current_user_dependency),  # noqa: B008
    ) -> JSONResponse:
        runs = _require_runs(repositories)
        session = await repositories.chat.get_session(owner_user_id=user.id, session_id=session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Chat session not found")
        content = body.content.strip()
        if not content:
            raise HTTPException(status_code=422, detail="Chat content is required")
        metadata = sanitize_chat_metadata(body.metadata)
        fingerprint = _request_fingerprint(content, metadata)
        configuration_snapshot: dict[str, object] = {}
        if agent_configuration_runtime is not None:
            resolved = await agent_configuration_runtime.resolve_snapshot(
                owner_user_id=user.id, node="conversation"
            )
            configuration_snapshot = agent_configuration_runtime.public_snapshot(resolved)
        try:
            created = await runs.create_or_get(
                owner_user_id=user.id,
                session_id=session_id,
                client_request_id=body.client_request_id,
                request_fingerprint=fingerprint,
                content=content,
                metadata=metadata,
                agent_configuration_snapshot=configuration_snapshot,
            )
        except Exception as exc:
            from super_ai.memory.repositories import ChatRunIdempotencyConflict

            if isinstance(exc, ChatRunIdempotencyConflict):
                raise HTTPException(status_code=409, detail="Client request conflict") from exc
            raise
        return _success(request, _run_payload(created.run), status_code=202)

    @router.get("/active")
    async def get_active_run(
        request: Request,
        session_id: str,
        user: UserRecord = Depends(current_user_dependency),  # noqa: B008
    ) -> JSONResponse:
        run = await _require_runs(repositories).get_active(
            owner_user_id=user.id, session_id=session_id
        )
        return _success(request, _run_payload(run) if run is not None else None)

    @router.get("/{run_id}")
    async def get_run(
        request: Request,
        session_id: str,
        run_id: str,
        user: UserRecord = Depends(current_user_dependency),  # noqa: B008
    ) -> JSONResponse:
        run = await _owned_run(repositories, user.id, session_id, run_id)
        return _success(request, _run_payload(run))

    @router.get("/{run_id}/events")
    async def stream_run_events(
        request: Request,
        session_id: str,
        run_id: str,
        user: UserRecord = Depends(current_user_dependency),  # noqa: B008
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        await _owned_run(repositories, user.id, session_id, run_id)
        after_sequence = _after_sequence(last_event_id)

        async def frames():  # type: ignore[no-untyped-def]
            cursor = after_sequence
            while True:
                run = await _owned_run(repositories, user.id, session_id, run_id)
                events = await _require_runs(repositories).list_events(
                    owner_user_id=user.id,
                    run_id=run_id,
                    after_sequence=cursor,
                )
                for event in events:
                    cursor = event.sequence
                    if event.event_type not in _PUBLIC_EVENT_TYPES:
                        continue
                    public = public_run_event(
                        sequence=event.sequence,
                        event_type=event.event_type,
                        payload=event.public_payload,
                        timestamp=event.created_at,
                    )
                    yield encode_run_sse(public)
                if run.status in _TERMINAL_STATUSES and cursor >= run.last_event_sequence:
                    return
                if await request.is_disconnected():
                    return
                await asyncio.sleep(0.2)

        return StreamingResponse(
            frames(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router


def create_pending_chat_actions_router(
    *,
    repositories: MemoryRepositories,
    current_user_dependency: Callable[..., object],
) -> APIRouter:
    router = APIRouter(tags=["chat"])

    def service() -> PendingChatActionService:
        if repositories.pending_chat_actions is None:
            raise RuntimeError("Pending Chat Action repository is required")
        return PendingChatActionService(repositories.pending_chat_actions)

    @router.get("/chat/sessions/{session_id}/actions/pending")
    async def list_pending_actions(
        request: Request,
        session_id: str,
        user: UserRecord = Depends(current_user_dependency),  # noqa: B008
    ) -> JSONResponse:
        session = await repositories.chat.get_session(
            owner_user_id=user.id,
            session_id=session_id,
        )
        if session is None:
            raise HTTPException(status_code=404, detail="Chat session not found")
        actions = await service().list_pending(
            owner_user_id=user.id,
            session_id=session_id,
        )
        return _success(request, {"items": [action.to_payload() for action in actions]})

    @router.post("/chat/actions/{action_id}/confirm")
    async def confirm_action(
        request: Request,
        action_id: str,
        user: UserRecord = Depends(current_user_dependency),  # noqa: B008
    ) -> JSONResponse:
        try:
            action = await service().confirm(owner_user_id=user.id, action_id=action_id)
        except PendingActionNotFound as exc:
            raise HTTPException(status_code=404, detail="Pending action not found") from exc
        return _success(request, action.to_payload())

    @router.post("/chat/actions/{action_id}/cancel")
    async def cancel_action(
        request: Request,
        action_id: str,
        user: UserRecord = Depends(current_user_dependency),  # noqa: B008
    ) -> JSONResponse:
        try:
            action = await service().cancel(owner_user_id=user.id, action_id=action_id)
        except PendingActionNotFound as exc:
            raise HTTPException(status_code=404, detail="Pending action not found") from exc
        return _success(request, action.to_payload())

    return router


async def _owned_run(
    repositories: MemoryRepositories,
    owner_user_id: str,
    session_id: str,
    run_id: str,
) -> ChatRunRecord:
    run = await _require_runs(repositories).get_owned(
        owner_user_id=owner_user_id,
        session_id=session_id,
        run_id=run_id,
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Chat run not found")
    return run


def _require_runs(repositories: MemoryRepositories):  # type: ignore[no-untyped-def]
    if repositories.chat_runs is None:
        raise RuntimeError("Chat run repository is required")
    return repositories.chat_runs


def _request_fingerprint(content: str, metadata: dict[str, object]) -> str:
    canonical = json.dumps(
        {"content": content, "metadata": metadata},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _after_sequence(value: str | None) -> int:
    if value is None or value == "":
        return 0
    try:
        return max(0, int(value))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid Last-Event-ID") from exc


def _run_payload(run: ChatRunRecord) -> dict[str, object]:
    snapshot = dict(run.agent_configuration_snapshot)
    if snapshot:
        snapshot["createdAt"] = run.created_at.isoformat()
    return {
        "id": run.id,
        "sessionId": run.session_id,
        "clientRequestId": run.client_request_id,
        "status": run.status,
        "lastEventSequence": run.last_event_sequence,
        "errorCode": run.error_code,
        "createdAt": run.created_at.isoformat(),
        "updatedAt": run.updated_at.isoformat(),
        "agentConfigurationSnapshot": snapshot or None,
    }


def _success(request: Request, data: object, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "ok": True,
            "data": data,
            "meta": {"requestId": getattr(request.state, "request_id", "unknown")},
        },
    )
