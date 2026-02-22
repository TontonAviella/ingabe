import asyncio
import os
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple
from urllib.parse import parse_qs, urlparse, urlunparse
import ipaddress
import asyncpg
from fastapi import HTTPException, status
import logging
import ssl

from src.structures import get_async_db_connection

logger = logging.getLogger(__name__)


class PostgresConnectionURIError(Exception):
    """Exception for user-friendly PostgreSQL URI validation errors."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(self.message)


class PostgresConfigurationError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(self.message)


class PostgresConnectionManager:
    def __init__(self):
        pass

    def verify_postgresql_uri(self, connection_uri: str) -> Tuple[str, bool]:
        """
        Verify that a PostgreSQL URI is valid and accessible.
        Returns (processed_uri, was_rewritten) tuple.
        Raises PostgresConnectionURIError with user-friendly messages.
        """
        connection_uri = connection_uri.strip()

        # Check basic format
        if not connection_uri.startswith("postgresql://"):
            raise PostgresConnectionURIError(
                "Invalid PostgreSQL connection URI format. Must start with 'postgresql://'"
            )

        # Parse the URI
        try:
            parsed = urlparse(connection_uri)
        except Exception as e:
            logger.debug("Failed to parse PostgreSQL connection URI: %s", e)
            raise PostgresConnectionURIError(
                "Invalid PostgreSQL connection URI format. Please check your connection string."
            )

        # Check if hostname is present
        if not parsed.hostname:
            raise PostgresConnectionURIError(
                "PostgreSQL connection URI must include a hostname."
            )

        # Check for localhost/loopback addresses
        host = parsed.hostname.lower()
        is_loopback = False

        # Check for literal "localhost"
        if host == "localhost":
            is_loopback = True
        else:
            # Check if it's a loopback IP address
            try:
                ip = ipaddress.ip_address(host)
                if ip.is_loopback:
                    is_loopback = True
            except ValueError:
                # Not an IP address, continue with other checks
                pass

        if is_loopback:
            # will fail if not set
            localhost_policy = os.environ.get("POSTGIS_LOCALHOST_POLICY")

            if localhost_policy == "disallow":
                raise PostgresConnectionURIError(
                    f"Detected a localhost database address ({host}) that Mundi cannot connect to. "
                )
            elif localhost_policy == "docker_rewrite":
                # Rewrite localhost to host.docker.internal for Docker environments
                new_parsed = parsed._replace(
                    netloc=parsed.netloc.replace(host, "host.docker.internal")
                )
                rewritten_uri = urlunparse(new_parsed)
                return rewritten_uri, True
            elif localhost_policy == "allow":
                # Allow localhost connections as-is
                return connection_uri, False
            else:
                logger.error(f"Unknown POSTGIS_LOCALHOST_POLICY: {localhost_policy}")
                raise PostgresConfigurationError(
                    f"Unknown POSTGIS_LOCALHOST_POLICY: {localhost_policy}"
                )

        return connection_uri, False

    async def get_connection(self, connection_id: str) -> Dict[str, Any]:
        """Get connection details by ID. Returns dict with connection data."""
        async with get_async_db_connection() as conn:
            connection = await conn.fetchrow(
                """
                SELECT id, project_id, user_id, connection_uri, connection_name,
                       created_at, updated_at, last_error_text, last_error_timestamp,
                       soft_deleted_at
                FROM project_postgres_connections
                WHERE id = $1
                """,
                connection_id,
            )
            if not connection:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Postgres connection {connection_id} not found",
                )
            return dict(connection)

    async def update_error_status(
        self, connection_id: str, error_text: Optional[str] = None
    ) -> None:
        """Update error status for a connection."""
        async with get_async_db_connection() as conn:
            if error_text:
                await conn.execute(
                    """
                    UPDATE project_postgres_connections
                    SET last_error_text = $1, last_error_timestamp = $2
                    WHERE id = $3
                    """,
                    error_text,
                    datetime.now(timezone.utc),
                    connection_id,
                )
            else:
                await conn.execute(
                    """
                    UPDATE project_postgres_connections
                    SET last_error_text = NULL, last_error_timestamp = NULL
                    WHERE id = $1
                    """,
                    connection_id,
                )

    async def connect_to_postgres(
        self, connection_id: str, timeout: float | None = None
    ) -> asyncpg.Connection:
        """Connect to a PostgreSQL database using the stored connection details."""
        if timeout is None:
            timeout = float(os.environ.get("MUNDI_POSTGIS_TIMEOUT_SEC", "10"))

        pg_connection = await self.get_connection(connection_id)
        uri = pg_connection["connection_uri"]

        # Respect sslmode=disable in URI (e.g. internal Docker connections)
        parsed_uri = urlparse(uri)
        qs = parse_qs(parsed_uri.query)
        ssl_disabled = qs.get("sslmode", [None])[0] == "disable"

        try:
            if ssl_disabled:
                # Connect without SSL for internal connections
                conn = await asyncio.wait_for(
                    asyncpg.connect(uri, ssl=False),
                    timeout=timeout,
                )
            else:
                # Create SSL context that accepts self-signed certificates
                ssl_context = ssl.create_default_context()
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE

                try:
                    conn = await asyncio.wait_for(
                        asyncpg.connect(uri, ssl=ssl_context),
                        timeout=timeout,
                    )
                except Exception as ssl_err:
                    # If SSL is rejected, retry without SSL
                    if "SSL" in str(ssl_err) or "rejected" in str(ssl_err):
                        logger.info(
                            "SSL rejected for connection %s, retrying without SSL",
                            connection_id,
                        )
                        conn = await asyncio.wait_for(
                            asyncpg.connect(uri, ssl=False),
                            timeout=timeout,
                        )
                    else:
                        raise

            # Make the connection read-only at the session level
            await conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")

            # Guard against runaway queries — cancels any single statement
            # that exceeds this limit.  Uses the same env var as the
            # connection timeout so operators have one knob to turn.
            stmt_timeout_ms = int(timeout * 1000 * 3)  # 3× connect timeout (default 30 s)
            await conn.execute(f"SET statement_timeout = {stmt_timeout_ms}")

            await self.update_error_status(connection_id, error_text=None)
            return conn
        except asyncio.TimeoutError:
            error_msg = f"Connection timeout after {timeout}s"
            await self.update_error_status(connection_id, error_msg)
            raise HTTPException(
                status_code=status.HTTP_408_REQUEST_TIMEOUT,
                detail=f"Failed to connect to postgres: {error_msg}",
            )
        except asyncpg.PostgresError as e:
            error_msg = f"Postgres error: {str(e)}"
            await self.update_error_status(connection_id, error_msg)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Failed to connect to postgres: {error_msg}",
            )
        except Exception as e:
            logger.error(f"Unexpected third-party asyncpg error: {str(e)}")
            error_msg = f"Unexpected error: {str(e)}"
            await self.update_error_status(connection_id, error_msg)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to connect to postgres: {error_msg}",
            )


def get_postgres_connection_manager() -> PostgresConnectionManager:
    """Get a PostgresConnectionManager instance."""
    return PostgresConnectionManager()
