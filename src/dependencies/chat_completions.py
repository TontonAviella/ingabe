from abc import ABC, abstractmethod
import os


class ChatArgsProvider(ABC):
    @abstractmethod
    async def get_args(self, user_uuid: str, route_name: str) -> dict:
        pass


class DefaultChatArgsProvider(ChatArgsProvider):
    async def get_args(self, user_uuid: str, route_name: str) -> dict:
        # feel free to customize below depending on which ollama model you're using
        # or whichever provider you want to use. you can also change depending on
        # user or route.
        model = os.environ.get("OPENAI_MODEL", "gpt-4.1-nano")
        return {"model": model}


def get_chat_args_provider() -> ChatArgsProvider:
    return DefaultChatArgsProvider()
