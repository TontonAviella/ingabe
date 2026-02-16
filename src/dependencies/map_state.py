from abc import ABC, abstractmethod
from typing import List, Dict, Any
from pydantic import BaseModel


class SelectedFeature(BaseModel):
    layer_id: str
    attributes: Dict[str, Any]


class MapStateProvider(ABC):
    @abstractmethod
    async def get_system_messages(
        self,
        messages: List[Dict[str, Any]],
        current_map_description: str,
        selected_feature: SelectedFeature | None,
    ) -> List[Dict[str, Any]]:
        pass


class DefaultMapStateProvider(MapStateProvider):
    async def get_system_messages(
        self,
        messages: List[Dict[str, Any]],
        current_map_description: str,
        selected_feature: SelectedFeature | None,
    ) -> List[Dict[str, Any]]:
        system_messages = []

        tagged_description = f"<MapState>\n{current_map_description}\n</MapState>"
        system_messages.append({"role": "system", "content": tagged_description})

        if selected_feature:
            selected_feature_content = f"<SelectedFeature>\n{selected_feature.model_dump_json()}\n</SelectedFeature>"
            system_messages.append(
                {"role": "system", "content": selected_feature_content}
            )
        else:
            system_messages.append(
                {"role": "system", "content": "<NoSelectedFeature />"}
            )

        return system_messages


def get_map_state_provider() -> MapStateProvider:
    return DefaultMapStateProvider()
