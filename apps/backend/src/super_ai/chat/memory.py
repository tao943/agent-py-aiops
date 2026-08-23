"""Session-scoped chat context measurement and compression."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, cast

from langchain_core.messages.utils import count_tokens_approximately

from super_ai.llm import LlmProvider
from super_ai.memory.repositories import (
    ChatMessageRecord,
    ChatSessionRecord,
    MemoryRepositories,
)

ChatMemoryMode = Literal["adaptive", "manual"]
ChatMemoryModeInput = Literal[
    "adaptive", "manual", "every_30_turns", "context_70_percent"
]
SUPPORTED_CHAT_MEMORY_MODES: tuple[ChatMemoryModeInput, ...] = (
    "adaptive",
    "every_30_turns",
    "context_70_percent",
    "manual",
)
AUTO_CONTEXT_THRESHOLD_PERCENT = 70.0
BACKGROUND_COMPACTION_THRESHOLD_PERCENT = 60.0
SYNCHRONOUS_COMPACTION_THRESHOLD_PERCENT = 85.0


class ChatContextLimitReached(RuntimeError):
    """Raised before persistence when a candidate message exceeds the hard budget."""


def normalize_memory_mode(value: str) -> ChatMemoryMode:
    """Normalize one-release legacy automatic modes to the adaptive policy."""
    if value in {"adaptive", "every_30_turns", "context_70_percent"}:
        return "adaptive"
    if value == "manual":
        return "manual"
    raise ValueError(f"Unsupported chat memory mode: {value}")


@dataclass(frozen=True, slots=True)
class PreparedChatContext:
    session: ChatSessionRecord
    messages: tuple[ChatMessageRecord, ...]
    system_prompt: str


class ChatMemoryService:
    """Apply a session memory policy without deleting persisted history."""

    def __init__(
        self,
        *,
        repositories: MemoryRepositories,
        llm_provider: LlmProvider,
        context_window_tokens: int,
    ) -> None:
        if context_window_tokens <= 0:
            raise ValueError("context_window_tokens must be positive")
        self._repositories = repositories
        self._llm_provider = llm_provider
        self.context_window_tokens = context_window_tokens

    async def prepare_message(
        self,
        *,
        owner_user_id: str,
        session: ChatSessionRecord,
        history: list[ChatMessageRecord],
        system_prompt: str,
        content: str,
        tool_schemas: Sequence[str] = (),
        observations: Sequence[object] = (),
    ) -> PreparedChatContext:
        current = session
        uncompressed = _uncompacted_history(history, current)
        candidate = _candidate_user_message(current, owner_user_id, content)
        candidate_messages = [*uncompressed, candidate]
        candidate_tokens = estimate_context_tokens(
            system_prompt=system_prompt,
            memory_summary=current.memory_summary,
            messages=candidate_messages,
        )
        completed_turns = sum(message.role == "assistant" for message in uncompressed)
        should_compact = (
            current.memory_mode == "every_30_turns" and completed_turns >= 30
        ) or (
            current.memory_mode == "context_70_percent"
            and _usage_percent(candidate_tokens, self.context_window_tokens)
            >= AUTO_CONTEXT_THRESHOLD_PERCENT
        )
        if should_compact and uncompressed:
            current = await self._compact_messages(
                owner_user_id=owner_user_id,
                session=current,
                messages=uncompressed,
                system_prompt=system_prompt,
            )
            candidate_messages = [candidate]
            candidate_tokens = estimate_context_tokens(
                system_prompt=system_prompt,
                memory_summary=current.memory_summary,
                messages=candidate_messages,
            )

        from super_ai.chat.context_envelope import (
            ContextEnvelopeRequest,
            ContextEnvelopeService,
        )
        synchronous_boundary = _older_history_boundary(uncompressed)
        if (
            _usage_percent(candidate_tokens, self.context_window_tokens)
            >= SYNCHRONOUS_COMPACTION_THRESHOLD_PERCENT
            and synchronous_boundary is not None
        ):
            from super_ai.chat.memory_jobs import StructuredMemoryCompactionHandler

            await StructuredMemoryCompactionHandler(
                repositories=self._repositories,
                llm_provider=self._llm_provider,
            ).compact(
                owner_user_id=owner_user_id,
                session_id=current.id,
                through_message_id=synchronous_boundary,
                expected_version=current.memory_summary_version,
            )
            current = (
                await self._repositories.chat.get_session(
                    owner_user_id=owner_user_id, session_id=current.id
                )
            ) or current
        from super_ai.chat.structured_memory import StructuredChatMemory

        envelope = ContextEnvelopeService().prepare(
            ContextEnvelopeRequest(
                system_prompt=system_prompt,
                messages=tuple(candidate_messages),
                structured_memory=StructuredChatMemory.model_validate(
                    current.structured_memory
                ),
                legacy_untrusted_summary=(
                    current.memory_summary
                    if current.memory_summary_version == 0
                    else None
                ),
                tool_schemas=tuple(tool_schemas),
                observations=tuple(observations),
                window_tokens=self.context_window_tokens,
                configured_output_min_tokens=min(
                    512, max(1, self.context_window_tokens // 10)
                ),
            )
        )
        candidate_tokens = (
            envelope.budget.system_tokens
            + envelope.budget.tool_schema_tokens
            + envelope.budget.memory_tokens
            + envelope.budget.recent_turn_tokens
            + envelope.budget.observation_tokens
        )

        updated = await self._repositories.chat.update_memory_state(
            owner_user_id=owner_user_id,
            session_id=current.id,
            context_tokens=candidate_tokens,
        )
        return PreparedChatContext(
            session=updated or current,
            messages=envelope.messages,
            system_prompt=envelope.system_prompt,
        )

    async def refresh_usage(
        self,
        *,
        owner_user_id: str,
        session: ChatSessionRecord,
        history: list[ChatMessageRecord],
        system_prompt: str,
    ) -> ChatSessionRecord:
        active_history = _uncompacted_history(history, session)
        structured_summary = (
            json.dumps(session.structured_memory, ensure_ascii=False, sort_keys=True)
            if session.memory_summary_version > 0
            else session.memory_summary
        )
        tokens = estimate_context_tokens(
            system_prompt=system_prompt,
            memory_summary=structured_summary,
            messages=active_history,
        )
        updated = await self._repositories.chat.update_memory_state(
            owner_user_id=owner_user_id,
            session_id=session.id,
            context_tokens=tokens,
        )
        current = updated or session
        await self._schedule_background_compaction(
            owner_user_id=owner_user_id,
            session=current,
            history=active_history,
        )
        return current

    async def set_mode(
        self,
        *,
        owner_user_id: str,
        session: ChatSessionRecord,
        mode: ChatMemoryModeInput,
        history: list[ChatMessageRecord],
        system_prompt: str,
    ) -> ChatSessionRecord:
        normalized_mode = normalize_memory_mode(mode)
        updated = await self._repositories.chat.update_memory_state(
            owner_user_id=owner_user_id,
            session_id=session.id,
            memory_mode=normalized_mode,
        )
        current = updated or session
        if normalized_mode == "manual":
            return await self.compact(
                owner_user_id=owner_user_id,
                session=current,
                history=history,
                system_prompt=system_prompt,
            )
        return await self.refresh_usage(
            owner_user_id=owner_user_id,
            session=current,
            history=history,
            system_prompt=system_prompt,
        )

    async def compact(
        self,
        *,
        owner_user_id: str,
        session: ChatSessionRecord,
        history: list[ChatMessageRecord],
        system_prompt: str,
    ) -> ChatSessionRecord:
        active_history = _uncompacted_history(history, session)
        if not active_history:
            return await self.refresh_usage(
                owner_user_id=owner_user_id,
                session=session,
                history=history,
                system_prompt=system_prompt,
            )
        from super_ai.chat.memory_jobs import StructuredMemoryCompactionHandler

        await StructuredMemoryCompactionHandler(
            repositories=self._repositories,
            llm_provider=self._llm_provider,
        ).compact(
            owner_user_id=owner_user_id,
            session_id=session.id,
            through_message_id=active_history[-1].id,
            expected_version=session.memory_summary_version,
        )
        current = (
            await self._repositories.chat.get_session(
                owner_user_id=owner_user_id, session_id=session.id
            )
        ) or session
        return await self.refresh_usage(
            owner_user_id=owner_user_id,
            session=current,
            history=history,
            system_prompt=system_prompt,
        )

    async def _compact_messages(
        self,
        *,
        owner_user_id: str,
        session: ChatSessionRecord,
        messages: list[ChatMessageRecord],
        system_prompt: str,
    ) -> ChatSessionRecord:
        transcript = "\n".join(
            f"{message.role}: {message.content}" for message in messages
        )
        prompt = (
            "请将以下对话压缩为可供后续模型继续对话的中文记忆摘要。保留用户目标、"
            "明确事实、偏好、决策、未完成事项、工具结果和引用来源；删除寒暄与重复内容。"
            "只输出摘要正文，不超过 1200 个汉字。\n\n"
            f"已有摘要：\n{session.memory_summary or '无'}\n\n"
            f"新增对话：\n{transcript}"
        )
        response = await self._llm_provider.create_chat_model().ainvoke(prompt)
        summary = _extract_model_text(response).strip()
        if not summary:
            raise RuntimeError("The model returned an empty memory summary.")
        compacted_count = session.compacted_message_count + len(messages)
        tokens = estimate_context_tokens(
            system_prompt=system_prompt,
            memory_summary=summary,
            messages=[],
        )
        updated = await self._repositories.chat.update_memory_state(
            owner_user_id=owner_user_id,
            session_id=session.id,
            memory_summary=summary,
            compacted_message_count=compacted_count,
            context_tokens=tokens,
            last_compacted_at=datetime.now(timezone.utc),
        )
        return updated or session

    async def _schedule_background_compaction(
        self,
        *,
        owner_user_id: str,
        session: ChatSessionRecord,
        history: list[ChatMessageRecord],
    ) -> None:
        if session.memory_mode != "adaptive" or self._repositories.background_jobs is None:
            return
        legacy_boundary = history[-1].id if session.memory_summary and history else None
        usage_boundary = (
            _older_history_boundary(history)
            if _usage_percent(session.context_tokens, self.context_window_tokens)
            >= BACKGROUND_COMPACTION_THRESHOLD_PERCENT
            else None
        )
        through_message_id = legacy_boundary or usage_boundary
        if through_message_id is None:
            return
        from super_ai.chat.memory_jobs import schedule_compaction

        await schedule_compaction(
            repositories=self._repositories,
            owner_user_id=owner_user_id,
            session=session,
            through_message_id=through_message_id,
        )


def estimate_context_tokens(
    *,
    system_prompt: str,
    memory_summary: str | None,
    messages: list[ChatMessageRecord],
) -> int:
    values: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    if memory_summary:
        values.append({"role": "system", "content": _memory_instruction(memory_summary)})
    values.extend(
        {"role": message.role, "content": message.content}
        for message in messages
        if message.role in {"user", "assistant"}
    )
    return int(count_tokens_approximately(cast(Any, values)))


def memory_payload(session: ChatSessionRecord, context_window_tokens: int) -> dict[str, object]:
    usage_percent = _usage_percent(session.context_tokens, context_window_tokens)
    return {
        "mode": normalize_memory_mode(session.memory_mode),
        "summaryVersion": session.memory_summary_version,
        "compactionStatus": session.memory_compaction_status,
        "contextTokens": session.context_tokens,
        "contextWindowTokens": context_window_tokens,
        "contextUsagePercent": usage_percent,
        "compactedMessageCount": session.compacted_message_count,
        "lastCompactedAt": (
            session.last_compacted_at.isoformat()
            if session.last_compacted_at is not None
            else None
        ),
        "canCompact": session.context_tokens > 0,
    }


def _candidate_user_message(
    session: ChatSessionRecord, owner_user_id: str, content: str
) -> ChatMessageRecord:
    return ChatMessageRecord(
        id="candidate",
        owner_user_id=owner_user_id,
        session_id=session.id,
        role="user",
        content=content,
        metadata={},
        created_at=datetime.now(timezone.utc),
    )


def _usage_percent(tokens: int, window: int) -> float:
    return round(min(100.0, tokens / window * 100), 1)


def _older_history_boundary(history: Sequence[ChatMessageRecord]) -> str | None:
    """Return the last message before the six newest user/assistant turns."""
    if len(history) <= 12:
        return None
    return history[-13].id


def _uncompacted_history(
    history: Sequence[ChatMessageRecord], session: ChatSessionRecord
) -> list[ChatMessageRecord]:
    if session.memory_through_message_id is not None:
        for index, message in enumerate(history):
            if message.id == session.memory_through_message_id:
                return list(history[index + 1 :])
    return list(history[session.compacted_message_count :])


def _memory_instruction(summary: str) -> str:
    return f"以下是此前对话的压缩记忆，请作为真实会话上下文继续回答：\n{summary}"


def _extract_model_text(value: object) -> str:
    content = getattr(value, "content", value)
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        parts: list[str] = []
        for item in cast(Sequence[object], content):
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping):
                text = cast(Mapping[object, object], item).get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""
