from abc import ABC, abstractmethod
from typing import Optional
import os
from fastapi import HTTPException, status, WebSocket
from fastapi.exceptions import WebSocketException


class UserContext(ABC):
    @abstractmethod
    def get_user_id(self) -> str:
        pass


# open source mundi has two modes, edit and read only.
# in edit mode, any user that accesses the application has access to all maps.
# in read only mode, maps designated as link accessible are accessible, allowing
# users to host maps publicly.
class EditOrReadOnlyUserContext(UserContext):
    def get_user_id(self) -> str:
        return "00000000-0000-0000-0000-000000000000"


def verify_session(session_required: bool = True):
    async def _verify_session() -> Optional[UserContext]:
        auth_mode = os.environ.get("MUNDI_AUTH_MODE")

        if auth_mode == "edit":
            return EditOrReadOnlyUserContext()
        elif auth_mode == "view_only":
            if session_required:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Authentication required",
                )
            return None
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="MUNDI_AUTH_MODE must be either 'edit' or 'view_only'",
            )

    return _verify_session


# Create specific functions for common usage patterns
async def verify_session_required(request=None) -> Optional[UserContext]:
    return await verify_session(session_required=True)()


async def verify_session_optional(request=None) -> Optional[UserContext]:
    return await verify_session(session_required=False)()


async def session_user_id(request=None) -> str:
    session = await verify_session_required(request)
    return session.get_user_id()


async def verify_websocket(websocket: WebSocket) -> UserContext:
    auth_mode = os.environ.get("MUNDI_AUTH_MODE")

    if auth_mode == "edit":
        return EditOrReadOnlyUserContext()
    elif auth_mode == "view_only":
        # deny access in view mode
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)
    else:
        raise WebSocketException(code=status.WS_1011_INTERNAL_ERROR)
