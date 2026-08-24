from __future__ import annotations

from super_ai.agent_configuration.runtime import AgentConfigurationRuntime
from super_ai.agent_configuration.service import AgentConfigurationService
from super_ai.memory.agent_configuration_sqlalchemy import (
    SQLAlchemyAgentConfigurationRepository,
)
from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.models import UserModel, utc_now


async def test_runtime_snapshot_intersects_tools_and_preserves_mandatory_prompt(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    async with session_factory() as session:
        now = utc_now()
        session.add(
            UserModel(
                id="owner_a",
                email="runtime-config@example.com",
                display_name="Runtime Config",
                password_hash="unused",
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()
    service = AgentConfigurationService(SQLAlchemyAgentConfigurationRepository(session_factory))
    runtime = AgentConfigurationRuntime(
        service,
        node_tool_allowlists={"conversation": frozenset({"query_incident", "search_knowledge"})},
    )
    try:
        _, prompt_draft = await service.create_resource(
            owner_user_id="owner_a",
            actor_user_id="owner_a",
            kind="prompt",
            name="AIOps assistant",
            description="Routes operational requests",
            content="优先关联正式 Incident，并区分事实与假设。",
            spec={"bindableNodes": ["conversation"]},
        )
        prompt = await service.publish_version(
            owner_user_id="owner_a",
            actor_user_id="owner_a",
            version_id=prompt_draft.id,
        )
        _, skill_draft = await service.create_resource(
            owner_user_id="owner_a",
            actor_user_id="owner_a",
            kind="skill",
            name="incident-investigation",
            description="Runs the formal incident loop",
            content="# Incident investigation\n\nStart, poll and close a formal investigation.",
            spec={
                "bindableNodes": ["conversation"],
                "allowedTools": ["query_incident", "ReadGroundTruth"],
                "risk": "read_only",
            },
        )
        skill = await service.publish_version(
            owner_user_id="owner_a",
            actor_user_id="owner_a",
            version_id=skill_draft.id,
        )
        await service.bind(
            owner_user_id="owner_a",
            actor_user_id="owner_a",
            node="conversation",
            prompt_version_id=prompt.id,
            skill_version_ids=[skill.id],
        )

        snapshot = await runtime.resolve_snapshot(owner_user_id="owner_a", node="conversation")
        assembled = runtime.assemble_system_prompt("MANDATORY SAFETY", snapshot)

        assert assembled.startswith("MANDATORY SAFETY")
        assert snapshot.allowed_tools == ("query_incident",)
        assert snapshot.policy_gate_required is True
        assert "ReadGroundTruth" not in assembled
        assert snapshot.skill_version_ids == (skill.id,)
        assert runtime.public_snapshot(snapshot)["promptVersionId"] == prompt.id
        assert "promptContent" not in runtime.public_snapshot(snapshot)
    finally:
        await engine.dispose()
