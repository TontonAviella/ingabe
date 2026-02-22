"""Backward-compatible re-exports from focused modules.

New code should import directly from the canonical locations:

* ``src.database.pool`` — connection pool and DB helpers
* ``src.models.messages`` — Pydantic message models and conversion helpers
"""

# ---------------------------------------------------------------------------
# Re-exports from src.database.pool
# ---------------------------------------------------------------------------
from src.database.pool import (  # noqa: F401
    IS_RUNNING_PYTEST,
    AsyncDatabaseConnection,
    _build_postgres_url,
    _get_async_connection_pool,
    _get_async_read_pool,
    async_conn,
    async_read_conn,
    get_async_db_connection,
    get_async_read_connection,
    tracer,
)

# ---------------------------------------------------------------------------
# Re-exports from src.models.messages
# ---------------------------------------------------------------------------
from src.models.messages import (  # noqa: F401
    TC_ICON_MAP,
    TC_TAGLINE_MAP,
    CodeBlock,
    SanitizedMessage,
    SanitizedToolCall,
    SanitizedToolResponse,
    convert_mundi_message_to_sanitized,
    convert_openai_tool_call_to_sanitized_tool_call,
    sanitized_fc_table_from_args,
)
