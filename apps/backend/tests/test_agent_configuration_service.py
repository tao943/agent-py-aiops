from __future__ import annotations

import pytest

from super_ai.agent_configuration.domain import (
    ConfigurationNotFound,
    InvalidBinding,
    PublishedVersionImmutable,
)
from super_ai.agent_configuration.service import AgentConfigurationService
from super_ai.memory.agent_configuration_sqlalchemy import (
    SQLAlchemyAgentConfigurationRepository,
)
from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.models import UserModel, utc_now


async def _service(database_url: str) -> tuple[object, AgentConfigurationService]:
    engine = create_memory_engine(database_url)
    session_factory = create_memory_session_factory(engine)
    async with session_factory() as session:
        now = utc_now()
        session.add_all(
            [
                UserModel(
                    id="owner_a",
                    email="agent-config-a@example.com",
                    display_name="Owner A",
                    password_hash="unused",
                    created_at=now,
                    updated_at=now,
                ),
                UserModel(
                    id="owner_b",
                    email="agent-config-b@example.com",
                    display_name="Owner B",
                    password_hash="unused",
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
        await session.commit()
    repository = SQLAlchemyAgentConfigurationRepository(session_factory)
    return engine, AgentConfigurationService(repository)


@pytest.mark.asyncio
async def test_published_version_is_immutable_and_owner_scoped(
    migrated_database_url: str,
) -> None:
    engine, service = await _service(migrated_database_url)
    try:
        resource, draft = await service.create_resource(
            owner_user_id="owner_a",
            actor_user_id="owner_a",
            kind="prompt",
            name="AIOps 运维协作助手",
            description="对话入口的路由与安全上下文",
            content="区分事实、假设、反证和未知。",
            spec={"bindableNodes": ["conversation"]},
        )
        published = await service.publish_version(
            owner_user_id="owner_a",
            actor_user_id="owner_a",
            version_id=draft.id,
        )

        assert published.status == "published"
        with pytest.raises(PublishedVersionImmutable):
            await service.update_draft(
                owner_user_id="owner_a",
                actor_user_id="owner_a",
                version_id=published.id,
                content="changed",
                spec={"bindableNodes": ["conversation"]},
            )
        with pytest.raises(ConfigurationNotFound):
            await service.get_resource(owner_user_id="owner_b", resource_id=resource.id)
    finally:
        await engine.dispose()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_skill_binding_cannot_target_core_aiops_nodes(
    migrated_database_url: str,
) -> None:
    engine, service = await _service(migrated_database_url)
    try:
        _, draft = await service.create_resource(
            owner_user_id="owner_a",
            actor_user_id="owner_a",
            kind="skill",
            name="incident-investigation",
            description="编排正式事件调查闭环",
            content="# Incident investigation\n\n按阶段执行调查。",
            spec={
                "bindableNodes": ["conversation"],
                "allowedTools": [],
                "risk": "read_only",
                "timeoutMs": 30000,
                "maxToolCalls": 8,
                "completionCriteria": ["formal incident linked"],
            },
        )
        published = await service.publish_version(
            owner_user_id="owner_a",
            actor_user_id="owner_a",
            version_id=draft.id,
        )

        with pytest.raises(InvalidBinding):
            await service.bind(
                owner_user_id="owner_a",
                actor_user_id="owner_a",
                node="planner",
                prompt_version_id=None,
                skill_version_ids=[published.id],
            )
        binding = await service.bind(
            owner_user_id="owner_a",
            actor_user_id="owner_a",
            node="conversation",
            prompt_version_id=None,
            skill_version_ids=[published.id],
        )
        assert binding.skill_version_ids == (published.id,)
    finally:
        await engine.dispose()  # type: ignore[attr-defined]
