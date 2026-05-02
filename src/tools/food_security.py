import asyncio

from pydantic import BaseModel, Field

from src.tools.pyd import IngabeToolCallMetaArgs


class GetFoodSecurityAlertsArgs(BaseModel):
    district: str = Field(
        ...,
        description="Rwanda district name to filter by. Pass empty string '' for all districts.",
    )
    period: str = Field(
        ...,
        description="Reporting period: 'current' for the present situation, 'projected' for forecast. Empty string '' defaults to 'current'.",
    )


async def get_food_security_alerts(
    args: GetFoodSecurityAlertsArgs, meta: IngabeToolCallMetaArgs
) -> dict:
    """Get FEWS NET food security alerts for Rwanda."""
    from src.services.fewsnet_service import get_food_security

    district = args.district.strip() if args.district else None
    period = (args.period.strip() or "current") if args.period else "current"

    return await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: get_food_security(
            district=district,
            period=period,
        ),
    )
