from __future__ import annotations

import asyncio

import pytest

from super_ai.agent_configuration.service import AgentConfigurationService
from super_ai.memory.agent_configuration_sqlalchemy import (
    SQLAlchemyAgentConfigurationRepository,
)
from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.models import UserModel, utc_now


@pytest.mark.asyncio
async def test_concurrent_publish_returns_one_immutable_version(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    async with session_factory() as session:
        now = utc_now()
        session.add(
            UserModel(
                id="owner_a",
                email="agent-config@example.com",
                display_name="Agent Config",
                password_hash="unused",
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()
    service = AgentConfigurationService(SQLAlchemyAgentConfigurationRepository(session_factory))
    try:
        _, draft = await service.create_resource(
            owner_user_id="owner_a",
            actor_user_id="owner_a",
            kind="prompt",
            name="Conversation router",
            description="Routes user intent",
            content="Route the request safely.",
            spec={"bindableNodes": ["conversation"]},
        )
        first, second = await asyncio.gather(
            *[
                service.publish_version(
                    owner_user_id="owner_a",
                    actor_user_id="owner_a",
                    version_id=draft.id,
                )
                for _ in range(2)
            ]
        )
        assert first.id == second.id
        assert first.status == second.status == "published"
    finally:
        await engine.dispose()
