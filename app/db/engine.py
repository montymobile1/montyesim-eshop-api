from contextlib import asynccontextmanager, contextmanager
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool, NullPool

from .settings import DATABASE_URL

# Tunable pool settings — adjust to your DB and workload
ASYNC_POOL_SIZE = 20
ASYNC_MAX_OVERFLOW = 40
SYNC_POOL_SIZE = 10
SYNC_MAX_OVERFLOW = 20

# Async engine: Don't specify poolclass, let SQLAlchemy use AsyncAdaptedQueuePool automatically
# This is the correct way to enable pooling for async engines
async_engine = create_async_engine(
    DATABASE_URL,
    echo=False,  # set True if you want verbose SQL logs
    pool_pre_ping=True,  # keeps connections healthy
    pool_size=ASYNC_POOL_SIZE,
    max_overflow=ASYNC_MAX_OVERFLOW,
    pool_recycle=3600,  # recycle connections after 1 hour
    # Optional: Connection arguments for asyncpg
    connect_args={
        "server_settings": {"jit": "off"},  # Can improve initial connection performance
        "command_timeout": 60,
    },
)

# Use AsyncSession explicitly and avoid expiring objects on commit (common for web apps)
SessionLocal = async_sessionmaker(bind=async_engine, class_=AsyncSession, autoflush=False, expire_on_commit=False)

# Convert async URL to sync URL for synchronous operations
SYNC_DATABASE_URL = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql+psycopg2://")

# Sync engine with pooling
sync_engine = create_engine(
    SYNC_DATABASE_URL,
    echo=False,  # set True if you want verbose SQL logs
    pool_pre_ping=True,  # keeps connections healthy
    poolclass=QueuePool,
    pool_size=SYNC_POOL_SIZE,
    max_overflow=SYNC_MAX_OVERFLOW,
)

# Use future=True and avoid expiring objects on commit
SyncSessionLocal = sessionmaker(bind=sync_engine, autoflush=False, autocommit=False, future=True, expire_on_commit=False)

@asynccontextmanager
async def get_async_session():
    """Async contextmanager that yields an AsyncSession.

    Use this in request handlers to create one session per request and reuse across repos.
    Example:
        async with get_async_session() as session:
            await repo.list(..., session=session)
    """
    async with SessionLocal() as session:
        try:
            yield session
        finally:
            # ensure connection returned to pool (for NullPool this closes immediately)
            await session.close()

@contextmanager
def get_sync_session():
    """Sync contextmanager that yields a sync Session (for synchronous repo methods)."""
    with SyncSessionLocal() as session:
        try:
            yield session
        finally:
            session.close()
