import asyncio

from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from radar.db.models import Base
from radar.settings import Settings

target_metadata = Base.metadata


def configure(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def online():
    engine = create_async_engine(Settings().database_url.get_secret_value())
    async with engine.connect() as connection:
        await connection.run_sync(configure)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url="postgresql://", target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(online())
