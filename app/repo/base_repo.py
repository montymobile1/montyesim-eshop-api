from typing import TypeVar, Generic, Type, List, Optional

from loguru import logger
from pydantic import BaseModel

from app.config.config import supabase_client
from app.config.db import DatabaseTables
from app.exceptions import DatabaseException

T = TypeVar("T", bound=BaseModel)


class BaseRepository(Generic[T]):

    def __init__(self, table_name: DatabaseTables, model: Type[T]):
        self.client = supabase_client()
        self.table = self.client.table(table_name)
        self.model = model

    def __handle_database_error(self, operation: str, error: Exception) -> None:
        """Centralized error handling for database operations"""
        error_msg = f"Database error during {operation}: {str(error)}"
        logger.error(error_msg)
        raise DatabaseException(error_msg)

    def select(self, tables: dict, where: dict = (), filters: dict = (), limit: int = 1000, offset: int = 0,
               order_by: str = None, desc=False,
               as_model: bool = True) -> List[T]:
        try:
            joins = " , ".join([f"{name}({keys})" for name, keys in tables.items()])
            select = f"*, {joins}" if len(joins) > 0 else "*"
            query = self.table.select(select)
            if len(where) > 0:
                for key, value in where.items():
                    query = query.eq(key, value)
            if len(filters) > 0:
                for key, value in filters.items():
                    query = query.filter(key, "eq", value)
            query = query.limit(limit).offset(offset)
            if order_by:
                query = query.order(order_by, desc=desc)
            response = query.execute()
            if not as_model:
                return response.data if response.data else []
            return [self.model(**item) for item in response.data] if response.data else []
        except Exception as e:
            self.__handle_database_error("select operation", e)
            return []

    def get_by_id(self, record_id: str) -> Optional[T]:
        try:
            response = self.table.select("*").eq("id", record_id).execute()
            return self.model(**response.data[0]) if response.data else None
        except Exception as e:
            raise DatabaseException(str(e))

    def select_procedure(self, where: dict = (), function_name: str = '') -> List[T]:
        try:
            response = self.client.rpc(function_name, params=where).execute()
            return [self.model(**item) for item in response.data] if response.data else []
        except Exception as e:
            self.__handle_database_error("procedure operation", e)
            return []

    def get_first_by(self, where: dict, filters: dict = None) -> Optional[T]:
        try:
            myquery = self.table.select("*")
            for key, value in where.items():
                myquery = myquery.eq(key, value)
            if filters:
                for key, value in filters.items():
                    myquery = myquery.filter(key, "eq", value)
            response = myquery.execute()
            return self.model(**response.data[0]) if response.data else None
        except Exception as e:
            self.__handle_database_error("get_first_by operation", e)
            return None

    def list(self, where: dict, limit: int = 1000, offset: int = 0, order_by: str = None, desc=False) -> List[T]:
        try:
            query = self.table.select("*")
            for key, value in where.items():
                query = query.eq(key, value)
            query = query.limit(limit).offset(offset)
            if order_by:
                query = query.order(order_by, desc=desc)
            response = query.execute()
            return [self.model(**item) for item in response.data] if response.data else []
        except Exception as e:
            self.__handle_database_error("list operation", e)
            return []

    def list_in(self, where: dict, filter: dict = (), limit: int = 1000, offset: int = 0, order_by: str = None,
                desc=False) -> List[T]:
        try:
            query = self.table.select("*")
            for key, value in where.items():
                query = query.eq(key, value)
            for key, value in filter.items():
                if isinstance(value, list):
                    value_str = f"({','.join(value)})"
                    query = query.filter(key, "in", value_str)
            query = query.limit(limit).offset(offset)
            if order_by:
                query = query.order(order_by, desc=desc)
            response = query.execute()
            return [self.model(**item) for item in response.data] if response.data else []
        except Exception as e:
            self.__handle_database_error("list_in operation", e)
            return []

    def create(self, data: dict) -> Optional[T]:
        try:
            response = self.table.insert(data).execute()
            return self.model(**response.data[0]) if response.data else None
        except Exception as e:
            self.__handle_database_error("create operation", e)
            return None

    def upsert(self, data: dict, on_conflict: str):
        try:
            response = self.table.upsert(data, on_conflict=on_conflict).execute()
            return response.data if response.data else None
        except Exception as e:
            self.__handle_database_error("upsert operation", e)
            return None

    def update(self, record_id: str, data: dict):
        try:
            response = self.table.update(data).eq("id", record_id).execute()
            return response.data if response.data else None
        except Exception as e:
            self.__handle_database_error("update operation", e)
            return None

    def update_by(self, where: dict, data: dict, filters: dict = None):
        try:
            myquery = self.table.update(data)

            for key, value in where.items():
                myquery = myquery.eq(key, value)
            if filters:
                for key, value in filters.items():
                    myquery = myquery.filter(key, "eq", value)
            response = myquery.execute()
            return response.data if response.data else None
        except Exception as e:
            self.__handle_database_error("update_by operation", e)
            return None

    def delete(self, record_id: str):
        try:
            response = self.table.delete().eq("id", record_id).execute()
            return response.data if response.data else None
        except Exception as e:
            self.__handle_database_error("delete operation", e)
            return None

    def delete_by(self, where: dict):
        try:
            query = self.table.delete()
            for key, value in where.items():
                query = query.eq(key, value)
            response = query.execute()
            return response.data if response.data else None
        except Exception as e:
            self.__handle_database_error("delete_by operation", e)
            return None
