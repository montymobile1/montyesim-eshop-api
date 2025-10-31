from typing import TypeVar, Generic, Type, Optional, Any

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, DeclarativeBase

from app.config.config import SessionLocal
from app.exceptions import DatabaseException

T = TypeVar("T", bound=DeclarativeBase)


class BaseRepository(Generic[T]):
    def __init__(self, model: Type[T]):
        self.model = model

    def get_session(self) -> Session:
        return SessionLocal()

    def get_by_id(self, record_id: Any) -> Optional[T]:
        with self.get_session() as session:
            try:
                stmt = select(self.model).where(self.model.id == record_id)
                return session.scalars(stmt).first()
            except SQLAlchemyError as e:
                raise DatabaseException(str(e))

    def get_first_by(self, where: dict) -> Optional[T]:
        with self.get_session() as session:
            try:
                stmt = select(self.model)
                for key, value in where.items():
                    stmt = stmt.where(getattr(self.model, key) == value)
                return session.scalars(stmt).first()
            except SQLAlchemyError as e:
                raise DatabaseException(str(e))

    def list(self, where: dict, limit: int = 50, offset: int = 0, order_by: dict = None) -> list[T]:
        with self.get_session() as session:
            try:
                stmt = select(self.model)
                for key, value in where.items():
                    stmt = stmt.where(getattr(self.model, key) == value)
                stmt = stmt.limit(limit).offset(offset)
                if order_by:
                    for key, desc in order_by.items():
                        stmt = stmt.order_by(
                            getattr(self.model, key).desc() if desc else getattr(self.model, key)
                        )
                return list(session.scalars(stmt))
            except SQLAlchemyError as e:
                raise DatabaseException(str(e))

    def create(self, data: dict) -> T:
        with self.get_session() as session:
            try:
                record = self.model(**data)
                session.add(record)
                session.commit()
                session.refresh(record)
                return record
            except SQLAlchemyError as e:
                session.rollback()
                raise DatabaseException(str(e))

    def update(self, record_id: Any, data: dict) -> Optional[T]:
        with self.get_session() as session:
            try:
                stmt = (
                    update(self.model)
                    .where(self.model.id == record_id)
                    .values(**data)
                    .returning(self.model)
                )
                result = session.execute(stmt)
                session.commit()
                return result.scalars().first()
            except SQLAlchemyError as e:
                session.rollback()
                raise DatabaseException(str(e))

    def update_by(self, where: dict, data: dict) -> Optional[T]:
        with self.get_session() as session:
            try:
                stmt = update(self.model).values(**data).returning(self.model)
                for key, value in where.items():
                    stmt = stmt.where(getattr(self.model, key) == value)
                result = session.execute(stmt)
                session.commit()
                return result.scalars().first()
            except SQLAlchemyError as e:
                session.rollback()
                raise DatabaseException(str(e))

    def delete(self, record_id: Any) -> None:
        with self.get_session() as session:
            try:
                record = session.get(self.model, record_id)
                if record:
                    session.delete(record)
                    session.commit()
            except SQLAlchemyError as e:
                session.rollback()
                raise DatabaseException(str(e))

    def delete_by(self, where: dict) -> None:
        with self.get_session() as session:
            try:
                stmt = select(self.model)
                for key, value in where.items():
                    stmt = stmt.where(getattr(self.model, key) == value)
                records = session.scalars(stmt).all()
                for record in records:
                    session.delete(record)
                session.commit()
            except SQLAlchemyError as e:
                session.rollback()
                raise DatabaseException(str(e))
