from fastapi import HTTPException, Depends, Path
from src.dependencies.session import UserContext, verify_session_required
from src.database.models import Conversation
from src.structures import async_conn


async def get_conversation(
    conversation_id: int = Path(...),
    session: UserContext = Depends(verify_session_required),
) -> Conversation:
    user_id = session.get_user_id()

    async with async_conn("get_conversation", user_id=user_id) as conn:
        conversation = await conn.fetchrow(
            """
            SELECT id, project_id, owner_uuid, title, created_at, updated_at, soft_deleted_at
            FROM conversations
            WHERE id = $1 AND owner_uuid = $2 AND soft_deleted_at IS NULL
            """,
            conversation_id,
            user_id,
        )
        if not conversation:
            raise HTTPException(404, f"Conversation {conversation_id} not found")

        return Conversation(
            id=conversation["id"],
            project_id=conversation["project_id"],
            owner_uuid=conversation["owner_uuid"],
            title=conversation["title"],
            created_at=conversation["created_at"],
            updated_at=conversation["updated_at"],
            soft_deleted_at=conversation["soft_deleted_at"],
        )


async def get_or_create_conversation(
    conversation_id: str = Path(...),
    map_id: str = Path(...),
    session: UserContext = Depends(verify_session_required),
) -> Conversation:
    user_id = session.get_user_id()

    async with async_conn("get_or_create_conversation", user_id=user_id) as conn:
        # Handle NEW conversation creation
        if conversation_id == "NEW":
            # Get project_id from map
            map_row = await conn.fetchrow(
                """
                SELECT project_id
                FROM user_mundiai_maps
                WHERE id = $1 AND owner_uuid = $2 AND soft_deleted_at IS NULL
                """,
                map_id,
                user_id,
            )
            if not map_row:
                raise HTTPException(404, f"Map {map_id} not found")

            # Create new conversation
            conversation = await conn.fetchrow(
                """
                INSERT INTO conversations (project_id, owner_uuid, title)
                VALUES ($1, $2, $3)
                RETURNING id, project_id, owner_uuid, title, created_at, updated_at, soft_deleted_at
                """,
                map_row["project_id"],
                user_id,
                "title pending",
            )

            return Conversation(
                id=conversation["id"],
                project_id=conversation["project_id"],
                owner_uuid=conversation["owner_uuid"],
                title=conversation["title"],
                created_at=conversation["created_at"],
                updated_at=conversation["updated_at"],
                soft_deleted_at=conversation["soft_deleted_at"],
            )

        # Handle existing conversation by ID
        try:
            conversation_id_int = int(conversation_id)
        except ValueError:
            raise HTTPException(400, f"Conversation {conversation_id} not found")

        # Verify conversation exists, user owns it, and map belongs to same project
        conversation = await conn.fetchrow(
            """
            SELECT c.id, c.project_id, c.owner_uuid, c.title, c.created_at, c.updated_at, c.soft_deleted_at
            FROM conversations c
            JOIN user_mundiai_maps m ON c.project_id = m.project_id
            WHERE c.id = $1 AND c.owner_uuid = $2 AND c.soft_deleted_at IS NULL
              AND m.id = $3 AND m.owner_uuid = $2 AND m.soft_deleted_at IS NULL
            """,
            conversation_id_int,
            user_id,
            map_id,
        )
        if not conversation:
            raise HTTPException(
                404, f"Conversation {conversation_id} or map {map_id} not found"
            )

        return Conversation(
            id=conversation["id"],
            project_id=conversation["project_id"],
            owner_uuid=conversation["owner_uuid"],
            title=conversation["title"],
            created_at=conversation["created_at"],
            updated_at=conversation["updated_at"],
            soft_deleted_at=conversation["soft_deleted_at"],
        )
