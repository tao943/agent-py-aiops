"""Resolve immutable, bounded Agent configuration for one execution."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import cast

from super_ai.agent_configuration.domain import (
    AgentConfigurationSnapshot,
    AgentNode,
    PublishedSkill,
)
from super_ai.agent_configuration.service import AgentConfigurationService


class AgentConfigurationRuntime:
    """Assemble published configuration without expanding server permissions."""

    def __init__(
        self,
        service: AgentConfigurationService,
        *,
        node_tool_allowlists: Mapping[AgentNode, frozenset[str]] | None = None,
    ) -> None:
        self._service = service
        self._node_tool_allowlists = dict(node_tool_allowlists or {})

    async def resolve_snapshot(
        self, *, owner_user_id: str, node: AgentNode
    ) -> AgentConfigurationSnapshot:
        binding = await self._service.get_binding(owner_user_id=owner_user_id, node=node)
        if binding is None:
            return AgentConfigurationSnapshot(
                owner_user_id=owner_user_id,
                node=node,
                prompt_version_id=None,
                prompt_content=None,
                skill_version_ids=(),
                skills=(),
                allowed_tools=(),
            )
        return await self._resolve_versions(
            owner_user_id=owner_user_id,
            node=node,
            prompt_version_id=binding.prompt_version_id,
            skill_version_ids=binding.skill_version_ids,
        )

    async def resolve_stored_snapshot(
        self,
        *,
        owner_user_id: str,
        node: AgentNode,
        stored: Mapping[str, object],
    ) -> AgentConfigurationSnapshot:
        prompt_value = stored.get("promptVersionId")
        prompt_version_id = prompt_value if isinstance(prompt_value, str) else None
        skill_version_ids = _string_list(stored.get("skillVersionIds"))
        return await self._resolve_versions(
            owner_user_id=owner_user_id,
            node=node,
            prompt_version_id=prompt_version_id,
            skill_version_ids=skill_version_ids,
        )

    async def _resolve_versions(
        self,
        *,
        owner_user_id: str,
        node: AgentNode,
        prompt_version_id: str | None,
        skill_version_ids: tuple[str, ...],
    ) -> AgentConfigurationSnapshot:
        prompt_content: str | None = None
        if prompt_version_id is not None:
            prompt = await self._service.get_version(
                owner_user_id=owner_user_id, version_id=prompt_version_id
            )
            prompt_content = prompt.content
        skills: list[PublishedSkill] = []
        requested_tools: set[str] = set()
        for skill_version_id in skill_version_ids:
            version = await self._service.get_version(
                owner_user_id=owner_user_id, version_id=skill_version_id
            )
            resource = await self._service.get_resource(
                owner_user_id=owner_user_id, resource_id=version.resource_id
            )
            requested_tools.update(_string_list(version.spec.get("allowedTools")))
            skills.append(
                PublishedSkill(
                    version_id=version.id,
                    name=resource.name,
                    description=resource.description,
                    content=version.content,
                    spec=version.spec,
                )
            )
        system_allowlist = self._node_tool_allowlists.get(node, frozenset())
        return AgentConfigurationSnapshot(
            owner_user_id=owner_user_id,
            node=node,
            prompt_version_id=prompt_version_id,
            prompt_content=prompt_content,
            skill_version_ids=skill_version_ids,
            skills=tuple(skills),
            allowed_tools=tuple(sorted(requested_tools & system_allowlist)),
        )

    def assemble_system_prompt(
        self, mandatory_system_prompt: str, snapshot: AgentConfigurationSnapshot
    ) -> str:
        sections = [mandatory_system_prompt.strip()]
        if snapshot.prompt_content:
            sections.append(
                "<untrusted-published-node-prompt>\n"
                + snapshot.prompt_content.strip()
                + "\n</untrusted-published-node-prompt>"
            )
        if snapshot.skills:
            catalog = "\n".join(
                f"- {skill.name}: {skill.description}" for skill in snapshot.skills
            )
            sections.append(
                "<untrusted-published-skill-catalog>\n"
                + catalog
                + "\n</untrusted-published-skill-catalog>"
            )
        return "\n\n".join(section for section in sections if section)

    def public_snapshot(self, snapshot: AgentConfigurationSnapshot) -> dict[str, object]:
        digests: dict[str, str] = {}
        if snapshot.prompt_version_id and snapshot.prompt_content:
            digests[snapshot.prompt_version_id] = _digest(snapshot.prompt_content)
        for skill in snapshot.skills:
            digests[skill.version_id] = _digest(skill.content)
        stable: dict[str, object] = {
            "node": snapshot.node,
            "promptVersionId": snapshot.prompt_version_id,
            "skillVersionIds": list(snapshot.skill_version_ids),
            "contentDigests": digests,
            "effectiveTools": list(snapshot.allowed_tools),
            "policyGateRequired": True,
        }
        return {"id": "agent_snapshot_" + _digest_json(stable)[:32], **stable}


def _string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in cast(list[object], value) if isinstance(item, str))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _digest_json(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _digest(encoded)
