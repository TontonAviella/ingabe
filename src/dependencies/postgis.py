from abc import ABC, abstractmethod
from functools import lru_cache
from src.dependencies.postgres_connection import PostgresConnectionManager
from src.dependencies.redis_client import get_redis_client

redis = get_redis_client()


class PostGISProvider(ABC):
    @abstractmethod
    async def get_tables_by_connection_id(
        self, connection_id: str, connection_manager: PostgresConnectionManager
    ) -> str:
        pass


class DefaultPostGISProvider(PostGISProvider):
    async def get_tables_by_connection_id(
        self, connection_id: str, connection_manager: PostgresConnectionManager
    ) -> str:
        cache_key = f"postgis:{connection_id}:tables"
        cached_result = redis.get(cache_key)
        if cached_result:
            return cached_result

        postgres_conn = await connection_manager.connect_to_postgres(connection_id)
        try:
            tables = await postgres_conn.fetch("""
                SELECT
                    t.table_name,
                    t.table_schema
                FROM information_schema.tables t
                WHERE t.table_schema NOT IN ('information_schema', 'pg_catalog', 'pg_toast')
                AND t.table_type = 'BASE TABLE'
                ORDER BY t.table_schema, t.table_name
            """)

            result = str([dict(table) for table in tables])

            redis.setex(cache_key, 3600, result)

            return result
        finally:
            await postgres_conn.close()


@lru_cache(maxsize=1)
def get_postgis_provider() -> PostGISProvider:
    return DefaultPostGISProvider()
