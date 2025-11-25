from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from .settings import DATABASE_URL

engine = create_async_engine(
    DATABASE_URL,
    echo=False,  # set True if you want verbose SQL logs
    pool_pre_ping=True,  # keeps connections healthy
)
SessionLocal = async_sessionmaker(bind=engine, autoflush=False, autocommit=False)
