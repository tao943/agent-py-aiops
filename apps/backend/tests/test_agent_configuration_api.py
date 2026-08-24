# pyright: reportUnusedFunction=false
from __future__ import annotations

from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Header, Request

from super_ai.agent_configuration.routes import create_agent_configuration_router
from super_ai.agent_configuration.service import AgentConfigurationService
from super_ai.api.responses import ApiErrorException, error_response
from super_ai.auth.repositories import UserRecord
from super_ai.memory.agent_configuration_sqlalchemy import (
    SQLAlchemyAgentConfigurationRepository,
)
from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.models import UserModel

NOW = datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc)


def _user(owner_id: str) -> UserRecord:
    return UserRecord(
        id=owner_id,
        email=f"{owner_id}@example.test",
        display_name=owner_id,
        password_hash="unused",
        created_at=NOW,
        updated_at=NOW,
    )


async def test_agent_configuration_api_lifecycle_and_owner_isolation(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    async with session_factory() as session:
        session.add_all(
            [
                UserModel(
                    id=owner,
                    email=f"{owner}@example.test",
                    display_name=owner,
                    password_hash="unused",
                    created_at=NOW,
                    updated_at=NOW,
                )
                for owner in ("owner_a", "owner_b")
            ]
        )
        await session.commit()
    service = AgentConfigurationService(SQLAlchemyAgentConfigurationRepository(session_factory))

    async def current_user(x_owner: str = Header(alias="x-owner")) -> UserRecord:
        return _user(x_owner)

    app = FastAPI()

    @app.exception_handler(ApiErrorException)
    async def handle_error(request: Request, exc: ApiErrorException) -> object:
        return error_response(request, exc.code, message=exc.message)

    app.include_router(
        create_agent_configuration_router(
            current_user_dependency=current_user,
            service=service,
        )
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            created = await client.post(
                "/agent-configuration/resources",
                headers={"x-owner": "owner_a"},
                json={
                    "kind": "prompt",
                    "name": "AIOps assistant",
                    "description": "Conversation router",
                    "content": "Associate a formal incident first.",
                    "spec": {"bindableNodes": ["conversation"]},
                },
            )
            version_id = created.json()["data"]["version"]["id"]
            published = await client.post(
                f"/agent-configuration/versions/{version_id}:publish",
                headers={"x-owner": "owner_a"},
            )
            cross_owner = await client.post(
                f"/agent-configuration/versions/{version_id}:publish",
                headers={"x-owner": "owner_b"},
            )
            library = await client.get(
                "/agent-configuration/resources", headers={"x-owner": "owner_a"}
            )
            audit = await client.get(
                "/agent-configuration/audit", headers={"x-owner": "owner_a"}
            )

        assert created.status_code == 201
        assert published.status_code == 200
        assert published.json()["data"]["version"]["status"] == "published"
        assert cross_owner.status_code == 404
        assert library.json()["data"]["capabilities"]["canManageConfiguration"] is True
        assert len(library.json()["data"]["resources"]) == 1
        assert {item["action"] for item in audit.json()["data"]["items"]} >= {
            "resource_created",
            "version_published",
        }
    finally:
        await engine.dispose()
