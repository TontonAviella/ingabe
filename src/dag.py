from pydantic import BaseModel, Field
from enum import Enum


class DAGEditOperationResponse(BaseModel):
    dag_child_map_id: str = Field(
        description="The ID of the new map created that contains the changes. Use this ID for further operations on the modified map."
    )
    dag_parent_map_id: str = Field(
        description="The ID of the original map which was copied to create the new map."
    )


class ForkReason(Enum):
    """Reason for forking a map"""

    USER_EDIT = "user_edit"
    AI_EDIT = "ai_edit"
