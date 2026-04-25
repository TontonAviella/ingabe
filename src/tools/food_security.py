import asyncio
from typing import Optional

from pydantic import BaseModel

from src.tools.pyd import IngabeToolCallMetaArgs


class GetFoodSecurityAlertsArgs(BaseModel):
    district: Optional[str] = None
    period: Optional[str] = "current"


async def get_food_security_alerts(
    args: GetFoodSecurityAlertsArgs, meta: IngabeToolCallMetaArgs
) -> dict:
    """Get FEWS NET food security alerts for Rwanda."""
    from src.services.fewsnet_service import get_food_security

    return await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: get_food_security(
            district=args.district,
            period=args.period or "current",
        ),
    )
