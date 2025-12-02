import aiocache
import json
from typing import TypeVar, Type, Optional, List, Union
from loguru import logger
from pydantic import BaseModel, TypeAdapter

T = TypeVar('T', bound=BaseModel)


class CacheService:

    @staticmethod
    async def add_to_cache(cache_key: str, data: Union[BaseModel, List[BaseModel]], ttl: int = 3600) -> None:
        """
        Store any Pydantic BaseModel or List[BaseModel] in cache.

        Args:
            cache_key: Unique key for storing the data
            data: Any Pydantic BaseModel instance or list of BaseModel instances
            ttl: Time to live in seconds (default: 3600 = 1 hour)

        Examples:
            # Single model
            await CacheService.add_to_cache("user:123", user_model, ttl=3600)

            # List of models
            await CacheService.add_to_cache("bundles:all:en", bundle_list, ttl=3600)
        """
        try:
            cache = aiocache.caches.get("default")

            # Serialize based on type
            if isinstance(data, list):
                # List of BaseModels
                json_data = json.dumps([item.model_dump() for item in data])
            else:
                # Single BaseModel
                json_data = data.model_dump_json()

            await cache.set(cache_key, json_data, ttl=ttl)
            logger.info(f"Stored data in cache with key: {cache_key}")
        except Exception as e:
            logger.error(f"Error storing data in cache: {e}")

    @staticmethod
    async def read_from_cache(cache_key: str, model_class: Type[T]) -> Optional[T]:
        """
        Retrieve and validate data from cache as a specific Pydantic model.

        Args:
            cache_key: The key to retrieve data for
            model_class: The Pydantic model class to validate against (e.g., BundleDTO)

        Returns:
            Instance of model_class if found and valid, None otherwise

        Example:
            bundle = await CacheService.read_from_cache("bundle_123", BundleDTO)
        """
        try:
            cached_data = await aiocache.caches.get("default").get(cache_key)
            if cached_data:
                logger.info(f"Retrieved data from cache with key: {cache_key}")
                return model_class.model_validate_json(cached_data)
            else:
                logger.info(f"No data found in cache for key: {cache_key}")
                return None
        except Exception as e:
            logger.error(f"Error reading from cache: {e}")
            return None

    @staticmethod
    async def read_list_from_cache(cache_key: str, model_class: Type[T]) -> Optional[List[T]]:
        """
        Retrieve and validate a list of data from cache as a specific Pydantic model.

        Args:
            cache_key: The key to retrieve data for
            model_class: The Pydantic model class to validate against (e.g., BundleDTO)

        Returns:
            List of model_class instances if found and valid, None otherwise

        Example:
            bundles = await CacheService.read_list_from_cache("bundles:all:en", BundleDTO)
        """
        try:
            cached_data = await aiocache.caches.get("default").get(cache_key)
            if cached_data:
                logger.info(f"Retrieved list data from cache with key: {cache_key}")
                # Use TypeAdapter for validating lists
                adapter = TypeAdapter(List[model_class])
                return adapter.validate_json(cached_data)
            else:
                logger.info(f"No data found in cache for key: {cache_key}")
                return None
        except Exception as e:
            logger.error(f"Error reading list from cache: {e}")
            return None

    @staticmethod
    async def delete_by_prefix(prefix: str) -> None:
        """
        Delete all cache keys that start with the given prefix.

        This uses the backend-specific `clear(namespace)` entry point provided by aiocache
        which will delete keys matching the namespace for Redis and memory backends.

        Args:
            prefix: Prefix of keys to delete. May include or omit a trailing ':' (both work).
        """
        try:
            # Normalize prefix so callers can pass either "home" or "home:"
            normalized = prefix[:-1] if prefix.endswith(":") else prefix
            cache = aiocache.caches.get("default")
            # The `clear(namespace)` call will use the backend implementation to
            # remove keys in that namespace (Redis uses namespace:* pattern).
            await cache.clear(normalized)
            logger.info(f"Cleared cache keys with prefix: {normalized}")
        except Exception as e:
            logger.error(f"Error deleting cache keys by prefix '{prefix}': {e}")
