from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker
import os
from dotenv import load_dotenv

load_dotenv()  # Load DATABASE_URL from .env

DATABASE_URL = os.getenv("DATABASE_URL")

# Example: postgresql+asyncpg://eshop_user:password@127.0.0.1:5432/e_sim_local
async_engine = create_async_engine(DATABASE_URL, echo=True, future=True)

# Typed sessionmaker for AsyncSession
async_session_maker: sessionmaker[AsyncSession] = sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

Base = declarative_base()
