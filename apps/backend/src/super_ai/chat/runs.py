"""Durable Conversation Agent run execution on the existing job runtime."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from super_ai.chat.aiops_bridge import BridgeResourceNotFound
from super_ai.chat.streaming import ChatStreamingService
from super_ai.jobs import BackgroundJobContext, TerminalBackgroundJobError
from super_ai.memory.repositories import ChatMessageRecord, JsonDict, MemoryRepositories


@dataclass(frozen=True, slots=True)
class ClassifiedChatError:
    code: str
    retryable: bool

    def public_payload(self, run_id: str) -> JsonDict:
        return {"code": self.code, "retryable": self.retryable, "runId": run_id}


class ChatRunJobHandler:
    """Execute one persisted run and project only public events into its event log."""

    def __init__(
        self,
        *,
        repositories: MemoryRepositories,
        streaming: ChatStreamingService,
        accessible_knowledge_base_ids: tuple[str, ...] = (),
    ) -> None:
        self._repositories = repositories
        self._streaming = streaming
        self._accessible_knowledge_base_ids = accessible_knowledge_base_ids

    async def __call__(self, context: BackgroundJobContext) -> None:
        runs = self._repositories.chat_runs
        if runs is None:
            raise TerminalBackgroundJobError("CHAT_RUN_REPOSITORY_UNAVAILABLE")
        owner_user_id = context.job.owner_user_id
        run_id = context.job.resource_id
        run = await runs.claim_attempt(owner_user_id=owner_user_id, run_id=run_id)
        if run is None:
            raise TerminalBackgroundJobError("CHAT_RUN_NOT_FOUND")
        if run.status == "succeeded":
            return
        if run.status in {"failed", "cancelled"}:
            raise TerminalBackgroundJobError(run.error_code or "CHAT_RUN_TERMINAL")

        assistant_message_id = f"message_{run.id}"
        existing_assistant = await self._repositories.chat.get_message(
            owner_user_id=owner_user_id,
            message_id=assistant_message_id,
        )
        if existing_assistant is not None:
            await runs.complete_with_event(
                owner_user_id=owner_user_id,
                run_id=run.id,
                assistant_message_id=assistant_message_id,
                public_payload={
                    "result": {"message": _message_payload(existing_assistant)},
                },
            )
            return

        if run.attempt_count == 1:
            await runs.append_event(
                owner_user_id=owner_user_id,
                run_id=run.id,
                event_type="run.status",
                public_payload={"runId": run.id, "status": "running"},
            )
        else:
            await runs.append_event(
                owner_user_id=owner_user_id,
                run_id=run.id,
                event_type="run.restarted",
                public_payload={"runId": run.id, "attempt": run.attempt_count},
            )

        session = await self._repositories.chat.get_session(
            owner_user_id=owner_user_id,
            session_id=run.session_id,
        )
        user_message = await self._repositories.chat.get_message(
            owner_user_id=owner_user_id,
            message_id=run.user_message_id,
        )
        if session is None or user_message is None:
            await self._fail_terminal(
                context, run.id, ClassifiedChatError("CHAT_RUN_INPUT_NOT_FOUND", False)
            )
            return

        complete_payload: JsonDict | None = None
        try:
            async for event in self._streaming.stream_message(
                owner_user_id=owner_user_id,
                session=session,
                content=user_message.content,
                metadata=user_message.metadata,
                accessible_knowledge_base_ids=(
                    self._accessible_knowledge_base_ids or (f"kb_{owner_user_id}",)
                ),
                existing_user_message_id=user_message.id,
                assistant_message_id=assistant_message_id,
                raise_errors=True,
            ):
                await context.raise_if_cancelled()
                event_type = str(event.get("type", ""))
                if event_type == "complete":
                    complete_payload = _public_event_payload(event)
                    continue
                if event_type == "error":
                    raise RuntimeError("chat stream returned a terminal error")
                await runs.append_event(
                    owner_user_id=owner_user_id,
                    run_id=run.id,
                    event_type=event_type,
                    public_payload=_public_event_payload(event),
                )
            assistant = await self._repositories.chat.get_message(
                owner_user_id=owner_user_id,
                message_id=assistant_message_id,
            )
            if assistant is None or complete_payload is None:
                raise RuntimeError("chat run did not produce a durable completion")
            await runs.complete_with_event(
                owner_user_id=owner_user_id,
                run_id=run.id,
                assistant_message_id=assistant.id,
                public_payload=complete_payload,
            )
        except Exception as exc:
            classified = classify_chat_error(exc)
            if classified.retryable and context.job.attempt < context.job.max_attempts:
                await runs.append_event(
                    owner_user_id=owner_user_id,
                    run_id=run.id,
                    event_type="run.attempt_failed",
                    public_payload=classified.public_payload(run.id),
                )
                raise
            await runs.fail_with_event(
                owner_user_id=owner_user_id,
                run_id=run.id,
                error_code=classified.code,
                public_payload=classified.public_payload(run.id),
            )
            if not classified.retryable:
                raise TerminalBackgroundJobError(classified.code) from exc
            raise

    async def _fail_terminal(
        self,
        context: BackgroundJobContext,
        run_id: str,
        classified: ClassifiedChatError,
    ) -> None:
        runs = self._repositories.chat_runs
        if runs is None:
            raise TerminalBackgroundJobError(classified.code)
        await runs.fail_with_event(
            owner_user_id=context.job.owner_user_id,
            run_id=run_id,
            error_code=classified.code,
            public_payload=classified.public_payload(run_id),
        )
        raise TerminalBackgroundJobError(classified.code)


def classify_chat_error(error: Exception) -> ClassifiedChatError:
    if isinstance(error, BridgeResourceNotFound | LookupError):
        return ClassifiedChatError("DIAGNOSTIC_NOT_FOUND", False)
    if isinstance(error, TimeoutError | ConnectionError):
        return ClassifiedChatError("BRIDGE_TOOL_TIMEOUT", True)
    return ClassifiedChatError("CHAT_AGENT_MODEL_FAILED", True)


def _public_event_payload(event: Mapping[str, object]) -> JsonDict:
    return {
        key: value
        for key, value in event.items()
        if key not in {"id", "type", "channel", "timestamp"}
    }


def _message_payload(message: ChatMessageRecord) -> JsonDict:
    return {
        "id": message.id,
        "content": message.content,
        "role": message.role,
    }
