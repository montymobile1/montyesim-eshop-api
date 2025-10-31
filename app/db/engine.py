from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from .settings import DATABASE_URL

engine = create_engine(
    DATABASE_URL,
    echo=False,          # set True if you want verbose SQL logs
    pool_pre_ping=True,  # keeps connections healthy
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
