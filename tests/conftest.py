import os

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Import models before touching Base.metadata so every table is registered.
from app import models  # noqa: F401
from app.db.session import Base

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://dns_switcher:dns_switcher@localhost:5432/dns_switcher_test",
)


@pytest_asyncio.fixture
async def session():
    # Function-scoped engine: pytest-asyncio gives each test its own event
    # loop by default, and asyncpg connections can't cross event loops, so
    # the engine must be created fresh (and torn down) within each test.
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as s:
        yield s

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()
