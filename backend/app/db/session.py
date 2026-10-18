from collections.abc import AsyncGenerator
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from backend.app.core.config import settings

from sqlalchemy.pool import NullPool

# 1. Asynchronous Database Engine for Governance & System State
gov_engine: AsyncEngine = create_async_engine(
    settings.GOVERNANCE_DATABASE_URL,
    echo=settings.DEBUG,
    poolclass=NullPool,
)

# 2. Asynchronous Database Engine for Business CRM Target
staging_engine: AsyncEngine = create_async_engine(
    settings.STAGING_DATABASE_URL,
    echo=settings.DEBUG,
    poolclass=NullPool,
)

# Session factories
AsyncGovSessionLocal = async_sessionmaker(
    bind=gov_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)

AsyncStagingSessionLocal = async_sessionmaker(
    bind=staging_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_gov_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency providing an async session for governance_db."""
    async with AsyncGovSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def get_staging_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency providing an async session for staging_db."""
    async with AsyncStagingSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def close_db_connections() -> None:
    """Disposes connection pools gracefully on application shutdown."""
    await gov_engine.dispose()
    await staging_engine.dispose()
