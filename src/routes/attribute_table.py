from fastapi import APIRouter, HTTPException, status, Depends
from src.dependencies.dag import get_layer
from src.database.models import MapLayer
from src.dependencies.session import verify_session_required, UserContext

attribute_table_router = APIRouter()


@attribute_table_router.get(
    "/layer/{layer_id}/attributes",
    operation_id="get_layer_attributes",
)
async def get_layer_attributes(
    offset: int = 0,
    limit: int = 100,
    layer: MapLayer = Depends(get_layer),
    session: UserContext = Depends(verify_session_required),
):
    if offset < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Offset must be non-negative",
        )

    if limit <= 0 or limit > 100:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Limit must be between 1 and 100",
        )

    async with await layer.get_ogr_source() as ogr_source:
        from osgeo import ogr, gdal

        gdal.UseExceptions()

        data_source = ogr.Open(ogr_source)
        if not data_source:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Could not open data source for layer {layer.layer_id}",
            )

        ogr_layer = data_source.GetLayer(0)
        if not ogr_layer:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"No layers found in data source for layer {layer.layer_id}",
            )

        feature_count = ogr_layer.GetFeatureCount()

        layer_def = ogr_layer.GetLayerDefn()
        field_names = []
        for i in range(layer_def.GetFieldCount()):
            field_def = layer_def.GetFieldDefn(i)
            field_names.append(field_def.GetName())

        ogr_layer.ResetReading()
        for _ in range(offset):
            feature = ogr_layer.GetNextFeature()
            if not feature:
                break

        features_data = []
        features_read = 0

        while features_read < limit:
            feature = ogr_layer.GetNextFeature()
            if not feature:
                break

            attributes = {}
            for field_name in field_names:
                field_value = feature.GetField(field_name)
                attributes[field_name] = field_value

            features_data.append(
                {"id": str(feature.GetFID()), "attributes": attributes}
            )

            features_read += 1

        has_more = False
        if features_read == limit:
            next_feature = ogr_layer.GetNextFeature()
            has_more = next_feature is not None

        return {
            "data": features_data,
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
            "total_count": feature_count if feature_count >= 0 else None,
            "field_names": field_names,
        }
