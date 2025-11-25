from typing import TypeVar, Generic, Type, Optional, Any, List

from sqlalchemy import select, update, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, selectinload

from app.db.engine import SessionLocal
from app.exceptions import DatabaseException

T = TypeVar("T", bound=DeclarativeBase)


class BaseRepository(Generic[T]):
    def __init__(self, model: Type[T]):
        self.model = model

    def get_session(self) -> AsyncSession:
        return SessionLocal()

    async def upsert(self, data: dict | T, on_conflict: str) -> T:
        """
        Perform an upsert (INSERT ... ON CONFLICT DO UPDATE) operation.

        Args:
            data: Dictionary or model instance containing the fields to insert/update
            on_conflict: Comma-separated string of column names that define the conflict constraint
                        e.g., "email" or "user_id,device_id"

        Returns:
            The inserted or updated model instance

        Example:
            await repo.upsert(
                data={"email": "test@example.com", "name": "Test User"},
                on_conflict="email"
            )
        """
        async with self.get_session() as session:
            try:
                # Parse conflict columns
                conflict_columns = [col.strip() for col in on_conflict.split(',')]

                # Convert model instance to dict if needed
                if isinstance(data, dict):
                    data_dict = data
                else:
                    # Get all column values from the model instance, excluding None values
                    data_dict = {
                        c.name: getattr(data, c.name, None)
                        for c in data.__table__.columns
                        if getattr(data, c.name, None) is not None
                    }

                # Create INSERT statement
                stmt = insert(self.model).values(**data_dict)

                # Add ON CONFLICT DO UPDATE clause
                # Get all columns to update (excluding the conflict columns)
                update_dict = {k: v for k, v in data_dict.items() if k not in conflict_columns}

                # Create the upsert statement
                upsert_stmt = stmt.on_conflict_do_update(
                    index_elements=conflict_columns,
                    set_=update_dict
                ).returning(self.model)

                # Execute and get result
                result = await session.execute(upsert_stmt)
                await session.commit()
                return result.scalars().first()
            except SQLAlchemyError as e:
                await session.rollback()
                raise DatabaseException(str(e))

    async def get_by_id(self, record_id: Any) -> Optional[T]:
        async with self.get_session() as session:
            try:
                stmt = select(self.model).where(self.model.id == record_id)
                result = await session.execute(stmt)
                return result.scalars().first()
            except SQLAlchemyError as e:
                raise DatabaseException(str(e))

    async def get_first_by(self, where: dict = None, filters: dict = None) -> Optional[T]:
        """
        Get first record matching WHERE conditions and/or raw SQL filters.

        Args:
            where: Dictionary of field:value pairs for exact matches (e.g., {"email": "test@example.com"})
            filters: Dictionary of raw SQL conditions for complex queries like JSONB operators
                    (e.g., {"metadata->>'referral_code'": "ABC123"})

        Returns:
            First matching record or None

        Example:
            # Simple WHERE
            user = await repo.get_first_by(where={"email": "test@example.com"})

            # JSONB filter
            user = await repo.get_first_by(filters={"metadata->>'referral_code'": "ABC123"})

            # Combined
            user = await repo.get_first_by(
                where={"email": "test@example.com"},
                filters={"metadata->>'country'": "USA"}
            )
        """
        async with self.get_session() as session:
            try:
                stmt = select(self.model)

                # Apply WHERE conditions
                if where:
                    for key, value in where.items():
                        stmt = stmt.where(getattr(self.model, key) == value)

                # Apply raw SQL filters for JSONB and other complex conditions
                if filters:
                    for condition, value in filters.items():
                        # Build the full condition with table name and placeholder
                        table_name = self.model.__tablename__
                        param_name = f"filter_{abs(hash(condition))}"
                        full_condition = f"{table_name}.{condition.strip()} = :{param_name}"
                        stmt = stmt.where(text(full_condition)).params(**{param_name: value})

                result = await session.execute(stmt)
                return result.scalars().first()
            except SQLAlchemyError as e:
                raise DatabaseException(str(e))

    async def list(self, where: dict = None, filters: dict = None, limit: int = 50, offset: int = 0,
                   order_by: str = None, desc: bool = False) -> List[T]:
        """
        List records with optional WHERE conditions and/or raw SQL filters.

        Args:
            where: Dictionary of field:value pairs for exact matches
            filters: Dictionary of raw SQL conditions for complex queries like JSONB operators
            limit: Maximum number of records to return
            offset: Number of records to skip
            order_by: Field name to order by (e.g., "created_at")
            desc: If True, order descending; if False, order ascending

        Returns:
            List of model instances

        Example:
            # Order by created_at descending
            items = await repo.list(
                where={"wallet_id": wallet_id},
                order_by="created_at",
                desc=True
            )
        """
        async with self.get_session() as session:
            try:
                stmt = select(self.model)

                # Apply WHERE conditions
                if where:
                    for key, value in where.items():
                        stmt = stmt.where(getattr(self.model, key) == value)

                # Apply raw SQL filters
                if filters:
                    for condition, value in filters.items():
                        table_name = self.model.__tablename__
                        param_name = f"filter_{abs(hash(condition))}"
                        full_condition = f"{table_name}.{condition.strip()} = :{param_name}"
                        stmt = stmt.where(text(full_condition)).params(**{param_name: value})

                # Apply ordering
                if order_by:
                    column = getattr(self.model, order_by)
                    stmt = stmt.order_by(column.desc() if desc else column)

                # Apply pagination
                stmt = stmt.limit(limit).offset(offset)

                result = await session.execute(stmt)
                return list(result.scalars())
            except SQLAlchemyError as e:
                raise DatabaseException(str(e))

    async def list_in(self, where: dict = None, filter: dict = None, limit: int = 50, offset: int = 0,
                      order_by: str = None, desc: bool = False) -> List[T]:
        """
        List records with optional WHERE conditions and IN filters.

        Args:
            where: Dictionary of field:value pairs for exact matches
            filter: Dictionary of field:[values] pairs for IN clause filtering
            limit: Maximum number of records to return
            offset: Number of records to skip
            order_by: Field name to order by (e.g., "created_at")
            desc: If True, order descending; if False, order ascending

        Returns:
            List of model instances
        """
        async with self.get_session() as session:
            try:
                stmt = select(self.model)

                # Apply WHERE conditions
                if where:
                    for key, value in where.items():
                        stmt = stmt.where(getattr(self.model, key) == value)

                # Apply IN filters
                if filter:
                    for key, values in filter.items():
                        if values:  # Only apply if list is not empty
                            stmt = stmt.where(getattr(self.model, key).in_(values))

                # Apply ordering
                if order_by:
                    column = getattr(self.model, order_by)
                    stmt = stmt.order_by(column.desc() if desc else column)

                # Apply pagination
                stmt = stmt.limit(limit).offset(offset)

                result = await session.execute(stmt)
                return list(result.scalars())
            except SQLAlchemyError as e:
                raise DatabaseException(str(e))

    async def create(self, data: dict | T) -> T:
        async with self.get_session() as session:
            try:
                # Handle both dict and model instance
                if isinstance(data, dict):
                    record = self.model(**data)
                else:
                    record = data

                session.add(record)
                await session.commit()
                await session.refresh(record)
                return record
            except SQLAlchemyError as e:
                await session.rollback()
                raise DatabaseException(str(e))

    async def update(self, record_id: Any, data: dict | T) -> Optional[T]:
        async with self.get_session() as session:
            try:
                # Convert model instance to dict if needed
                if isinstance(data, dict):
                    data_dict = data
                else:
                    # Get all column values from the model instance, excluding None values
                    data_dict = {
                        c.name: getattr(data, c.name, None)
                        for c in data.__table__.columns
                        if getattr(data, c.name, None) is not None
                    }

                stmt = (
                    update(self.model)
                    .where(self.model.id == record_id)
                    .values(**data_dict)
                    .returning(self.model)
                )
                result = await session.execute(stmt)
                await session.commit()
                return result.scalars().first()
            except SQLAlchemyError as e:
                await session.rollback()
                raise DatabaseException(str(e))

    async def update_by(self, where: dict, data: dict | T, filters: dict = None) -> Optional[T]:
        async with self.get_session() as session:
            try:
                # Convert model instance to dict if needed
                if isinstance(data, dict):
                    data_dict = data
                else:
                    # Get all column values from the model instance, excluding None values
                    data_dict = {
                        c.name: getattr(data, c.name, None)
                        for c in data.__table__.columns
                        if getattr(data, c.name, None) is not None
                    }

                stmt = update(self.model).values(**data_dict).returning(self.model)
                for key, value in where.items():
                    stmt = stmt.where(getattr(self.model, key) == value)
                if filters:
                    for condition, value in filters.items():
                        table_name = self.model.__tablename__
                        param_name = f"filter_{abs(hash(condition))}"
                        full_condition = f"{table_name}.{condition.strip()} = :{param_name}"
                        stmt = stmt.where(text(full_condition)).params(**{param_name: value})

                result = await session.execute(stmt)
                await session.commit()
                return result.scalars().first()
            except SQLAlchemyError as e:
                await session.rollback()
                raise DatabaseException(str(e))

    async def delete(self, record_id: Any) -> None:
        async with self.get_session() as session:
            try:
                record = await session.get(self.model, record_id)
                if record:
                    await session.delete(record)
                    await session.commit()
            except SQLAlchemyError as e:
                await session.rollback()
                raise DatabaseException(str(e))

    async def delete_by(self, where: dict) -> None:
        async with self.get_session() as session:
            try:
                stmt = select(self.model)
                for key, value in where.items():
                    stmt = stmt.where(getattr(self.model, key) == value)
                result = await session.execute(stmt)
                records = result.scalars().all()
                for record in records:
                    await session.delete(record)
                await session.commit()
            except SQLAlchemyError as e:
                await session.rollback()
                raise DatabaseException(str(e))
