"""FastAPI dependency for BrainService.

Provides a singleton BrainService instance via get_brain_service().
"""

from functools import lru_cache

from src.services.brain_service import BrainService


@lru_cache(maxsize=1)
def get_brain_service() -> BrainService:
    return BrainService()
