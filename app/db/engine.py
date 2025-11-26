from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import sessionmaker

from .settings import DATABASE_URL

async_engine = create_async_engine(
    DATABASE_URL,
    echo=False,  # set True if you want verbose SQL logs
    pool_pre_ping=True,  # keeps connections healthy
)
SessionLocal = async_sessionmaker(bind=async_engine, autoflush=False, autocommit=False)

# Convert async URL to sync URL for synchronous operations
SYNC_DATABASE_URL = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql+psycopg2://")

sync_engine = create_engine(
    SYNC_DATABASE_URL,
    echo=False,  # set True if you want verbose SQL logs
    pool_pre_ping=True,  # keeps connections healthy
)
SyncSessionLocal = sessionmaker(bind=sync_engine, autoflush=False, autocommit=False)
