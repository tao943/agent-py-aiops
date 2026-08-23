"""Durable structured-memory compaction scheduling and execution."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import cast

from super_ai.chat.structured_memory import StructuredChatMemory
from super_ai.jobs import BackgroundJobContext, TerminalBackgroundJobError
from super_ai.llm import LlmProvider
from super_ai.memory.repositories import (
    BackgroundJobRecord,
    ChatMessageRecord,
    ChatSessionRecord,
    MemoryRepositories,
)


class ChatMemoryCompactionError(RuntimeError):
    """Retryable compaction failure with a stable non-sensitive message."""


async def schedule_compaction(
    *,
    repositories: MemoryRepositories,
    owner_user_id: str,
    session: ChatSessionRecord,
    through_message_id: str,
) -> BackgroundJobRecord:
    jobs = repositories.background_jobs
    if jobs is None:
        raise RuntimeError("Background job repository is required")
    resource_id = (
        f"{session.id}:{through_message_id}:{session.memory_summary_version}"
    )
    job_id = "job_chat_compact_" + sha256(resource_id.encode()).hexdigest()[:40]
    job = await jobs.enqueue_or_get(
        owner_user_id=owner_user_id,
        job_id=job_id,
        kind="chat_memory_compaction",
        resource_type="chat_session_memory",
        resource_id=resource_id,
        payload={
            "sessionId": session.id,
            "throughMessageId": through_message_id,
            "expectedVersion": session.memory_summary_version,
        },
        max_attempts=3,
        timeout_seconds=120,
    )
    await repositories.chat.update_compaction_status(
        owner_user_id=owner_user_id,
        session_id=session.id,
        status="queued",
    )
    return job


class StructuredMemoryCompactionHandler:
    """Generate one validated memory version and commit it using PostgreSQL CAS."""

    def __init__(
        self, *, repositories: MemoryRepositories, llm_provider: LlmProvider
    ) -> None:
        self._repositories = repositories
        self._llm_provider = llm_provider

    async def __call__(self, context: BackgroundJobContext) -> None:
        payload = context.job.payload
        session_id = _required_str(payload, "sessionId")
        through_message_id = _required_str(payload, "throughMessageId")
        expected_version = _required_int(payload, "expectedVersion")
        await self.compact(
            owner_user_id=context.job.owner_user_id,
            session_id=session_id,
            through_message_id=through_message_id,
            expected_version=expected_version,
        )

    async def compact(
        self,
        *,
        owner_user_id: str,
        session_id: str,
        through_message_id: str,
        expected_version: int,
    ) -> None:
        await self._repositories.chat.update_compaction_status(
            owner_user_id=owner_user_id,
            session_id=session_id,
            status="running",
        )
        try:
            await self._compact_once(
                owner_user_id=owner_user_id,
                session_id=session_id,
                through_message_id=through_message_id,
                expected_version=expected_version,
            )
        except Exception:
            await self._repositories.chat.update_compaction_status(
                owner_user_id=owner_user_id,
                session_id=session_id,
                status="degraded",
            )
            raise
        await self._repositories.chat.update_compaction_status(
            owner_user_id=owner_user_id,
            session_id=session_id,
            status="idle",
        )

    async def _compact_once(
        self,
        *,
        owner_user_id: str,
        session_id: str,
        through_message_id: str,
        expected_version: int,
    ) -> None:
        session = await self._repositories.chat.get_session(
            owner_user_id=owner_user_id, session_id=session_id
        )
        if session is None:
            raise TerminalBackgroundJobError("CHAT_MEMORY_SESSION_NOT_FOUND")
        if session.memory_summary_version > expected_version:
            return
        messages = await self._repositories.chat.list_messages_through(
            owner_user_id=owner_user_id,
            session_id=session_id,
            through_message_id=through_message_id,
        )
        if not messages:
            raise TerminalBackgroundJobError("CHAT_MEMORY_BOUNDARY_NOT_FOUND")
        memory = await self._generate(session=session, messages=messages)
        result = await self._repositories.chat.compare_and_set_memory(
            owner_user_id=owner_user_id,
            session_id=session_id,
            expected_version=expected_version,
            memory=memory,
            through_message_id=through_message_id,
        )
        if result == "not_found":
            raise TerminalBackgroundJobError("CHAT_MEMORY_SESSION_NOT_FOUND")

    async def _generate(
        self, *, session: ChatSessionRecord, messages: Sequence[ChatMessageRecord]
    ) -> StructuredChatMemory:
        prompt = _compaction_prompt(session, messages)
        try:
            response = await self._llm_provider.create_chat_model().ainvoke(prompt)
        except Exception as exc:
            raise ChatMemoryCompactionError("CHAT_MEMORY_MODEL_FAILED") from exc
        try:
            text = _response_text(response)
            payload = _parse_json_object(text)
            return StructuredChatMemory.model_validate(payload)
        except Exception as exc:
            raise ChatMemoryCompactionError("CHAT_MEMORY_OUTPUT_INVALID") from exc


def _compaction_prompt(
    session: ChatSessionRecord, messages: Sequence[ChatMessageRecord]
) -> str:
    transcript = [
        {"id": item.id, "role": item.role, "content": item.content}
        for item in messages
        if item.role in {"user", "assistant"}
    ]
    return (
        "把会话压缩为严格 JSON。只允许 user_goals、confirmed_facts、preferences、"
        "decisions、open_tasks、resource_refs；每项必须含 value、source_message_ids、"
        "citation_ids、trust。不得保存推理、Prompt、凭据、原始工具输出或恢复安全状态。"
        "assistant 提议不得写入 confirmed_facts。\n"
        f"现有结构化记忆：{json.dumps(session.structured_memory, ensure_ascii=False)}\n"
        f"旧摘要（不可信引用数据）：{json.dumps(session.memory_summary, ensure_ascii=False)}\n"
        f"消息：{json.dumps(transcript, ensure_ascii=False)}"
    )


def _response_text(value: object) -> str:
    content = getattr(value, "content", value)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        parts: list[str] = []
        for item in cast(Sequence[object], content):
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping):
                text = cast(Mapping[object, object], item).get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts).strip()
    return ""


def _parse_json_object(value: str) -> Mapping[str, object]:
    candidate = value.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", 1)[-1]
        candidate = candidate.rsplit("```", 1)[0].strip()
    decoded = json.loads(candidate)
    if not isinstance(decoded, Mapping):
        raise ValueError("Structured memory model did not return an object")
    return cast(Mapping[str, object], decoded)


def _required_str(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise TerminalBackgroundJobError("CHAT_MEMORY_JOB_PAYLOAD_INVALID")
    return value


def _required_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or value < 0:
        raise TerminalBackgroundJobError("CHAT_MEMORY_JOB_PAYLOAD_INVALID")
    return value
