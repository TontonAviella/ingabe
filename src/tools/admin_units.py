"""list_admin_units: Rwanda's districts, sectors, cells and villages by name.

Answers "list the sectors in Huye" or "how many villages does Ruhashya have?"
from the admin boundary tables, without the model writing SQL.
"""

from typing import Literal

from pydantic import BaseModel, Field

from src.services.admin_boundaries import list_admin_units as _list_admin_units
from src.tools.pyd import IngabeToolCallMetaArgs


class ListAdminUnitsArgs(BaseModel):
    level: Literal["district", "sector", "cell", "village"] = Field(
        ..., description="Which units to list: 'district', 'sector', 'cell' or 'village'."
    )
    district: str = Field(
        ...,
        description="District the units are in, e.g. 'Huye'. '' when listing all 30 districts.",
    )
    sector: str = Field(
        ...,
        description="Sector to list the cells or villages of, e.g. 'Ruhashya'. '' if not needed.",
    )
    cell: str = Field(
        ..., description="Cell to list the villages of, e.g. 'Karama'. '' if not needed."
    )


async def list_admin_units(args: ListAdminUnitsArgs, meta: IngabeToolCallMetaArgs) -> dict:
    """List Rwanda's districts, or the sectors, cells or villages inside a named district, sector or cell, with an exact count. Use for "list the sectors in Huye", "how many cells does Nyanza have?", "which villages are in Ruhashya sector?". Sector and cell names repeat across Rwanda, so pass the district too. To draw the units on the map, use new_layer_from_postgis instead."""
    from src.structures import get_async_read_connection

    try:
        async with get_async_read_connection() as conn:
            result = await _list_admin_units(
                conn, args.level, district=args.district, sector=args.sector, cell=args.cell
            )
    except ValueError as e:
        return {"status": "error", "error": str(e)}
    return {"status": "success", **result}
