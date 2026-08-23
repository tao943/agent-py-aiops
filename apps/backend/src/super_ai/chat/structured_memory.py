"""Strict, provenance-carrying memory persisted for chat context reuse."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MemoryTrust = Literal[
    "user_asserted", "user_confirmed", "tool_grounded", "assistant_proposed"
]
MemoryCasResult = Literal["updated", "stale", "not_found"]

MAX_MEMORY_ENTRIES_PER_CATEGORY = 50
MAX_MEMORY_BYTES = 65_536
_CONFIRMED_FACT_TRUST = frozenset({"user_confirmed", "tool_grounded"})
_INSTRUCTION_PATTERNS = (
    "ignore previous",
    "ignore all rules",
    "system prompt",
    "developer message",
    "忽略规则",
    "忽略之前",
    "提升权限",
    "绕过权限",
)


class MemoryEntry(BaseModel):
    """One bounded memory fact with explicit provenance and trust."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str = Field(min_length=1, max_length=2_000)
    source_message_ids: tuple[str, ...] = Field(min_length=1, max_length=32)
    citation_ids: tuple[str, ...] = Field(default=(), max_length=32)
    trust: MemoryTrust


class StructuredChatMemory(BaseModel):
    """Allowlisted long-term conversation memory; never stores reasoning or safety state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    user_goals: tuple[MemoryEntry, ...] = Field(
        default=(), max_length=MAX_MEMORY_ENTRIES_PER_CATEGORY
    )
    confirmed_facts: tuple[MemoryEntry, ...] = Field(
        default=(), max_length=MAX_MEMORY_ENTRIES_PER_CATEGORY
    )
    preferences: tuple[MemoryEntry, ...] = Field(
        default=(), max_length=MAX_MEMORY_ENTRIES_PER_CATEGORY
    )
    decisions: tuple[MemoryEntry, ...] = Field(
        default=(), max_length=MAX_MEMORY_ENTRIES_PER_CATEGORY
    )
    open_tasks: tuple[MemoryEntry, ...] = Field(
        default=(), max_length=MAX_MEMORY_ENTRIES_PER_CATEGORY
    )
    resource_refs: tuple[MemoryEntry, ...] = Field(
        default=(), max_length=MAX_MEMORY_ENTRIES_PER_CATEGORY
    )

    @model_validator(mode="after")
    def validate_trust_and_size(self) -> StructuredChatMemory:
        for entry in self.confirmed_facts:
            if entry.trust not in _CONFIRMED_FACT_TRUST:
                raise ValueError(
                    "confirmed_facts require user_confirmed or tool_grounded trust"
                )
            normalized = entry.value.casefold()
            if any(pattern in normalized for pattern in _INSTRUCTION_PATTERNS):
                raise ValueError("instruction-like text cannot become a confirmed fact")
        encoded = json.dumps(
            self.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > MAX_MEMORY_BYTES:
            raise ValueError("structured memory exceeds the persisted byte limit")
        return self


class StructuredMemoryUpdate(BaseModel):
    """Validated input for one optimistic structured-memory update."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    memory: StructuredChatMemory
    through_message_id: str = Field(min_length=1, max_length=80)
