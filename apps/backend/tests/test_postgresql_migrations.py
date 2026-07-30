from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


async def test_alembic_head_exists_in_postgresql(migrated_database_url: str) -> None:
    engine = create_async_engine(migrated_database_url)
    async with engine.connect() as connection:
        dialect = await connection.scalar(text("select current_setting('server_version_num')"))
        revision = await connection.scalar(text("select version_num from alembic_version"))
    await engine.dispose()
    assert int(dialect) >= 160000
    assert revision
