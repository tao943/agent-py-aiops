"""Domain records and stable errors for Agent configuration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypeAlias

AgentNode: TypeAlias = Literal[
    "conversation",
    "planner",
    "replanner",
    "investigator_runtime",
    "investigator_log",
    "investigator_change",
    "adjudicator",
    "validator",
    "recovery_planner",
    "report",
]
ResourceKind: TypeAlias = Literal["prompt", "skill"]
VersionStatus: TypeAlias = Literal["draft", "published", "deprecated"]

AGENT_NODES: tuple[AgentNode, ...] = (
    "conversation",
    "planner",
    "replanner",
    "investigator_runtime",
    "investigator_log",
    "investigator_change",
    "adjudicator",
    "validator",
    "recovery_planner",
    "report",
)


class AgentConfigurationError(RuntimeError):
    """Base stable configuration error."""


class ConfigurationNotFound(AgentConfigurationError):
    """The owner-scoped resource does not exist or is not visible."""


class PublishedVersionImmutable(AgentConfigurationError):
    """Published and deprecated versions cannot be edited."""


class InvalidBinding(AgentConfigurationError):
    """A binding violates node, kind, owner or lifecycle constraints."""


class InvalidConfiguration(AgentConfigurationError):
    """Configuration content or structured spec is invalid."""


@dataclass(frozen=True, slots=True)
class AgentResourceRecord:
    id: str
    owner_user_id: str
    kind: ResourceKind
    name: str
    description: str
    legacy_resource_id: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AgentVersionRecord:
    id: str
    resource_id: str
    owner_user_id: str
    version: int
    status: VersionStatus
    content: str
    spec: dict[str, object]
    content_sha256: str
    validation_warnings: tuple[str, ...]
    created_at: datetime
    updated_at: datetime
    published_at: datetime | None
    deprecated_at: datetime | None


@dataclass(frozen=True, slots=True)
class AgentBindingRecord:
    id: str
    owner_user_id: str
    node: AgentNode
    prompt_version_id: str | None
    skill_version_ids: tuple[str, ...]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AgentAuditEventRecord:
    event_id: str
    owner_user_id: str
    actor_user_id: str
    action: str
    resource_id: str | None
    version_id: str | None
    node: AgentNode | None
    safe_metadata: dict[str, object]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PublishedSkill:
    version_id: str
    name: str
    description: str
    content: str
    spec: dict[str, object]


@dataclass(frozen=True, slots=True)
class AgentConfigurationSnapshot:
    owner_user_id: str
    node: AgentNode
    prompt_version_id: str | None
    prompt_content: str | None
    skill_version_ids: tuple[str, ...]
    skills: tuple[PublishedSkill, ...]
    allowed_tools: tuple[str, ...]
    policy_gate_required: bool = True
