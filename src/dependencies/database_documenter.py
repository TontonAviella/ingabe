import logging
from abc import ABC, abstractmethod
from functools import lru_cache
from typing import Tuple

logger = logging.getLogger(__name__)
from src.structures import get_async_db_connection
from src.dependencies.postgres_connection import PostgresConnectionManager
from src.dependencies.chat_completions import ChatArgsProvider
from src.dependencies.redis_client import get_redis_client
from src.utils import generate_id
from openai import AsyncOpenAI

redis = get_redis_client()


class DatabaseDocumenter(ABC):
    @abstractmethod
    async def generate_documentation(
        self,
        connection_id: str,
        connection_uri: str,
        connection_name: str,
        connection_manager: PostgresConnectionManager,
        openai_client: AsyncOpenAI,
        chat_args_provider: ChatArgsProvider,
        user_id: str,
    ) -> Tuple[str, str]:
        """
        Generate database documentation and friendly name.
        Returns tuple of (friendly_name, documentation_markdown)
        """
        pass


class DefaultDatabaseDocumenter(DatabaseDocumenter):
    async def generate_documentation(
        self,
        connection_id: str,
        connection_uri: str,
        connection_name: str,
        connection_manager: PostgresConnectionManager,
        openai_client: AsyncOpenAI,
        chat_args_provider: ChatArgsProvider,
        user_id: str,
    ) -> Tuple[str, str]:
        """
        Generate basic database documentation and friendly name.
        This function analyzes the PostgreSQL database schema (equivalent to \\d+)
        and generates a friendly display name and simple documentation.
        """
        try:
            # Establish a connection using the connection manager for proper error tracking
            conn = await connection_manager.connect_to_postgres(connection_id)
            try:
                # Get all tables
                tables = await conn.fetch(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                    ORDER BY table_name
                """
                )

                redis.set(f"dbdocumenter:{connection_id}:total_tables", len(tables))
                redis.set(f"dbdocumenter:{connection_id}:processed_tables", 0)

                # Build schema description
                schema_description = f"Database: {connection_name}\n\n"

                table_names = []
                for table in tables:
                    table_name = table["table_name"]
                    table_names.append(table_name)

                    # Get columns (similar to \d+ output)
                    columns = await conn.fetch(
                        """
                        SELECT
                            column_name,
                            data_type,
                            is_nullable,
                            column_default
                        FROM information_schema.columns
                        WHERE table_schema = 'public' AND table_name = $1
                        ORDER BY ordinal_position
                    """,
                        table_name,
                    )

                    schema_description += f"Table: {table_name}\n"
                    schema_description += "Columns:\n"

                    for col in columns:
                        nullable = "" if col["is_nullable"] == "YES" else " NOT NULL"
                        default = (
                            f" DEFAULT {col['column_default']}"
                            if col["column_default"]
                            else ""
                        )
                        schema_description += f"  {col['column_name']} - {col['data_type']}{nullable}{default}\n"

                    schema_description += "\n"

                    redis.incr(f"dbdocumenter:{connection_id}:processed_tables")
            finally:
                # Ensure the connection is closed to avoid leaks
                await conn.close()

            # Generate friendly name
            name_prompt = f"""Based on the following database tables, generate a short, friendly display name (2-4 words) that describes what this database contains or its purpose.

Database Name: {connection_name}
Tables: {", ".join(table_names)}

Respond with ONLY the friendly name, no additional text."""

            name_chat_args = await chat_args_provider.get_args(
                user_id, "generate_database_docs"
            )
            name_response = await openai_client.chat.completions.create(
                **name_chat_args,
                messages=[
                    {"role": "user", "content": name_prompt},
                ],
            )

            friendly_name = name_response.choices[0].message.content.strip()

            # Generate documentation
            system_prompt = """You are a database documentation expert. Create a brief overview of a PostgreSQL database.

Start immediately with a description beginning with "This PostgreSQL database...".

Be concise - just 2-3 paragraphs describing:
1. What the database appears to contain based on table names
2. The main purpose or domain it serves
3. Key tables and their likely relationships

Do not use any markdown headers."""

            user_prompt = f"""Create a brief overview for this PostgreSQL database:

Database Name: {connection_name}

Schema:
{schema_description}"""

            doc_chat_args = await chat_args_provider.get_args(
                user_id, "generate_database_docs"
            )
            response = await openai_client.chat.completions.create(
                **doc_chat_args,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )

            documentation = response.choices[0].message.content

            # Save the generated summary to the new table
            summary_id = generate_id(prefix="S")
            table_count = len(table_names)
            async with get_async_db_connection() as doc_conn:
                await doc_conn.execute(
                    """
                    INSERT INTO project_postgres_summary
                    (id, connection_id, friendly_name, summary_md, table_count)
                    VALUES ($1, $2, $3, $4, $5)
                """,
                    summary_id,
                    connection_id,
                    friendly_name,
                    documentation,
                    table_count,
                )

            logger.info(
                "Successfully generated documentation and friendly name for connection %s",
                connection_id,
            )

            return friendly_name, documentation

        except Exception as e:
            logger.warning(
                "Error generating database documentation for connection %s: %s",
                connection_id, e,
            )
            # Don't raise the exception - background tasks should fail silently as requested
            # Connection manager already handled error reporting
            return None, None


@lru_cache(maxsize=1)
def get_database_documenter() -> DatabaseDocumenter:
    return DefaultDatabaseDocumenter()
