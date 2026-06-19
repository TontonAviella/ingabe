import csv
import fiona
import io
import json
import logging
import time
from abc import ABC, abstractmethod
from functools import lru_cache
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

from src.structures import get_async_db_connection, get_async_read_connection

fiona.drvsupport.supported_drivers["WFS"] = "r"


def _coerce_layer_metadata(layer_data: Dict[str, Any]) -> Dict[str, Any]:
    metadata = layer_data.get("metadata") or layer_data.get("metadata_json") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, ValueError):
            return {}
    return metadata if isinstance(metadata, dict) else {}


def _is_geoparquet_backed_vector(
    layer_data: Dict[str, Any], metadata: Dict[str, Any]
) -> bool:
    if str(metadata.get("analytics_format", "")).lower() == "geoparquet":
        return True

    storage_candidates = [
        layer_data.get("s3_key"),
        metadata.get("geoparquet_key"),
        metadata.get("analytics_key"),
        metadata.get("analytics_store_key"),
    ]
    return any(
        "geoparquet/" in str(candidate).lower()
        or str(candidate).lower().endswith((".parquet", ".geoparquet"))
        for candidate in storage_candidates
        if candidate
    )


def _format_bounds(bounds: Any) -> Optional[str]:
    if not bounds or len(bounds) < 4:
        return None
    try:
        return f"{bounds[0]:.6f},{bounds[1]:.6f},{bounds[2]:.6f},{bounds[3]:.6f}"
    except (TypeError, ValueError):
        return None


class LayerDescriber(ABC):
    @abstractmethod
    async def describe_layer(self, layer_id: str, layer_data: Dict[str, Any]) -> str:
        pass

    @abstractmethod
    async def describe_postgis_layer(self, layer_data: Dict[str, Any]) -> List[str]:
        pass

    @abstractmethod
    async def describe_raster_layer(self, layer_data: Dict[str, Any]) -> List[str]:
        pass

    @abstractmethod
    async def describe_point_cloud_layer(self, layer_data: Dict[str, Any]) -> List[str]:
        pass

    @abstractmethod
    async def describe_vector_layer(
        self, layer_id: str, layer_data: Dict[str, Any]
    ) -> List[str]:
        pass


class DefaultLayerDescriber(LayerDescriber):
    async def describe_layer(self, layer_id: str, layer_data: Dict[str, Any]) -> str:
        markdown_content = []
        markdown_content.append(f"# Layer: {layer_data['name']}\n")
        markdown_content.append(f"ID: {layer_id}")
        markdown_content.append(f"Type: {layer_data['type']}")

        if layer_data["type"] == "postgis":
            postgis_content = await self.describe_postgis_layer(layer_data)
            markdown_content.extend(postgis_content)
        elif layer_data["type"] == "raster":
            raster_content = await self.describe_raster_layer(layer_data)
            markdown_content.extend(raster_content)
        elif layer_data["type"] == "point_cloud":
            point_cloud_content = await self.describe_point_cloud_layer(layer_data)
            markdown_content.extend(point_cloud_content)
        else:
            vector_content = await self.describe_vector_layer(layer_id, layer_data)
            markdown_content.extend(vector_content)

        return "\n".join(markdown_content)

    async def describe_postgis_layer(self, layer_data: Dict[str, Any]) -> List[str]:
        markdown_content = []

        async with get_async_db_connection() as conn:
            connection_result = await conn.fetchrow(
                """
                SELECT ppc.connection_name, ppc.connection_uri, pps.friendly_name
                FROM project_postgres_connections ppc
                LEFT JOIN project_postgres_summary pps ON pps.connection_id = ppc.id
                WHERE ppc.id = $1
                """,
                layer_data.get("postgis_connection_id"),
            )
            if connection_result:
                connection_name = (
                    connection_result["friendly_name"]
                    or connection_result["connection_name"]
                    or "Loading..."
                )
                markdown_content.append(f"PostGIS Connection: {connection_name}")

                # Use cached geometry analysis if available and fresh (< 1 hour)
                _GEOM_CACHE_TTL = 3600  # seconds
                metadata = layer_data.get("metadata") or layer_data.get("metadata_json") or {}
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except (json.JSONDecodeError, ValueError):
                        metadata = {}

                cached_geom = metadata.get("postgis_geom_types")
                cached_ts = metadata.get("postgis_geom_types_ts", 0)
                cache_fresh = (time.time() - cached_ts) < _GEOM_CACHE_TTL if cached_ts else False

                if cached_geom and cache_fresh:
                    markdown_content.append("\n## Geometry Types\n")
                    for entry in cached_geom:
                        markdown_content.append(
                            f"{entry['type']}: {entry['count']} features"
                        )
                else:
                    try:
                        geom_type_query = f"""
                        SELECT ST_GeometryType(geom) as geom_type, COUNT(*) as count
                        FROM ({layer_data.get("postgis_query", "SELECT NULL as geom")}) t
                        WHERE geom IS NOT NULL
                        GROUP BY ST_GeometryType(geom)
                        ORDER BY count DESC
                        """

                        from src.dependencies.db_pool import get_pooled_connection
                        async with get_pooled_connection(
                            connection_result["connection_uri"]
                        ) as postgis_conn:
                            geom_results = await postgis_conn.fetch(geom_type_query)

                        if geom_results:
                            geom_entries = [
                                {"type": row["geom_type"].replace("ST_", ""), "count": row["count"]}
                                for row in geom_results
                            ]
                            markdown_content.append("\n## Geometry Types\n")
                            for entry in geom_entries:
                                markdown_content.append(
                                    f"{entry['type']}: {entry['count']} features"
                                )

                            # Persist to metadata for future requests
                            try:
                                layer_id = layer_data.get("layer_id")
                                if layer_id:
                                    updated_meta = dict(metadata)
                                    updated_meta["postgis_geom_types"] = geom_entries
                                    updated_meta["postgis_geom_types_ts"] = int(time.time())
                                    async with get_async_db_connection() as meta_conn:
                                        await meta_conn.execute(
                                            "UPDATE map_layers SET metadata = $1 WHERE layer_id = $2",
                                            json.dumps(updated_meta),
                                            layer_id,
                                        )
                            except Exception:
                                logger.debug("Failed to cache PostGIS geometry analysis", exc_info=True)

                    except Exception as e:
                        logger.debug("Geometry type analysis failed: %s", e)
                        markdown_content.append(
                            f"Geometry Type: Unable to analyze ({str(e)})"
                        )

        markdown_content.append(f"Query: {layer_data.get('postgis_query', '???')}")
        markdown_content.append(
            f"Created On: {str(layer_data['created_on']) if layer_data['created_on'] else 'Unknown'}"
        )
        markdown_content.append(
            f"Last Edited: {str(layer_data['last_edited']) if layer_data['last_edited'] else 'Unknown'}"
        )

        return markdown_content

    async def describe_raster_layer(self, layer_data: Dict[str, Any]) -> List[str]:
        markdown_content = []

        markdown_content.append(
            f"Created On: {str(layer_data['created_on']) if layer_data['created_on'] else 'Unknown'}"
        )
        markdown_content.append(
            f"Last Edited: {str(layer_data['last_edited']) if layer_data['last_edited'] else 'Unknown'}"
        )

        if layer_data["bounds"]:
            markdown_content.append("\n## Geographic Extent\n")
            markdown_content.append(
                f"Bounds (WGS84): {layer_data['bounds'][0]:.6f},{layer_data['bounds'][1]:.6f},{layer_data['bounds'][2]:.6f},{layer_data['bounds'][3]:.6f}"
            )

        if layer_data["metadata"]:
            # Parse metadata JSON if it's a string (asyncpg returns JSON as strings)
            metadata = layer_data["metadata"]
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except (json.JSONDecodeError, TypeError):
                    metadata = {}

            if metadata and "raster_value_stats_b1" in metadata:
                markdown_content.append("\n## Raster Statistics\n")
                min_val = metadata["raster_value_stats_b1"]["min"]
                max_val = metadata["raster_value_stats_b1"]["max"]
                markdown_content.append(f"Min Value: {min_val}")
                markdown_content.append(f"Max Value: {max_val}")

        return markdown_content

    async def describe_point_cloud_layer(self, layer_data: Dict[str, Any]) -> List[str]:
        markdown_content = []

        markdown_content.append(
            f"Created On: {str(layer_data['created_on']) if layer_data['created_on'] else 'Unknown'}"
        )
        markdown_content.append(
            f"Last Edited: {str(layer_data['last_edited']) if layer_data['last_edited'] else 'Unknown'}"
        )

        if layer_data["bounds"]:
            markdown_content.append("\n## Geographic Extent\n")
            markdown_content.append(
                f"Bounds (WGS84): {layer_data['bounds'][0]:.6f},{layer_data['bounds'][1]:.6f},{layer_data['bounds'][2]:.6f},{layer_data['bounds'][3]:.6f}"
            )

        if layer_data["metadata"]:
            # Parse metadata JSON if it's a string (asyncpg returns JSON as strings)
            metadata = layer_data["metadata"]
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except (json.JSONDecodeError, TypeError):
                    metadata = {}

        return markdown_content

    def describe_vector_layer_from_metadata(
        self,
        layer_data: Dict[str, Any],
        metadata: Dict[str, Any],
        fallback_reason: Optional[str] = None,
    ) -> List[str]:
        markdown_content = []

        markdown_content.append("\n## Geographic Extent\n")
        formatted_bounds = _format_bounds(layer_data.get("bounds"))
        if formatted_bounds:
            markdown_content.append(f"Dataset Bounds: {formatted_bounds}")
        else:
            markdown_content.append("Dataset Bounds: Unknown")

        markdown_content.append("\n## Schema Information\n")
        if _is_geoparquet_backed_vector(layer_data, metadata):
            markdown_content.append("Driver: GeoParquet metadata summary")
            markdown_content.append("Analytics Store: GeoParquet")
            if metadata.get("pmtiles_key"):
                markdown_content.append("Browser Transport: PMTiles")
            if metadata.get("pmtiles_maxzoom") is not None:
                markdown_content.append(f"PMTiles Max Zoom: {metadata['pmtiles_maxzoom']}")
            if metadata.get("geoparquet_key"):
                markdown_content.append(f"GeoParquet Key: {metadata['geoparquet_key']}")
        else:
            markdown_content.append("Driver: Metadata summary")

        if fallback_reason:
            markdown_content.append(f"Reader Fallback: {fallback_reason}")

        h3_resolutions = metadata.get("h3_resolutions")
        if h3_resolutions:
            markdown_content.append(f"H3 Resolutions: {', '.join(map(str, h3_resolutions))}")

        resolution_cell_counts = metadata.get("resolution_cell_counts")
        if isinstance(resolution_cell_counts, dict) and resolution_cell_counts:
            counts = ", ".join(
                f"r{resolution}: {count}"
                for resolution, count in sorted(
                    resolution_cell_counts.items(), key=lambda item: str(item[0])
                )
            )
            markdown_content.append(f"Resolution Cell Counts: {counts}")

        if metadata.get("source_layer_id"):
            markdown_content.append(f"Source Raster Layer: {metadata['source_layer_id']}")
        if metadata.get("analysis_kind"):
            markdown_content.append(f"Analysis Kind: {metadata['analysis_kind']}")
        if metadata.get("screening_model"):
            markdown_content.append(f"Screening Model: {metadata['screening_model']}")
        if metadata.get("domain"):
            markdown_content.append(f"Domain: {metadata['domain']}")

        markdown_content.append("\n### Attribute Fields\n")
        inferred_fields = [
            "h3_index",
            "h3_resolution",
            "domain",
            "risk_score",
            "risk_level",
            "likely_issue",
            "recommended_action",
        ]
        markdown_content.extend(inferred_fields)

        return markdown_content

    async def describe_vector_layer(
        self, layer_id: str, layer_data: Dict[str, Any]
    ) -> List[str]:
        markdown_content = []
        metadata = _coerce_layer_metadata(layer_data)

        markdown_content.append(
            f"Geometry Type: {layer_data['geometry_type'] if layer_data['geometry_type'] else 'Unknown'}"
        )
        if layer_data["feature_count"] is not None:
            markdown_content.append(f"Feature Count: {layer_data['feature_count']}")
        markdown_content.append(
            f"Created On: {str(layer_data['created_on']) if layer_data['created_on'] else 'Unknown'}"
        )
        markdown_content.append(
            f"Last Edited: {str(layer_data['last_edited']) if layer_data['last_edited'] else 'Unknown'}"
        )

        # Get layer object to use get_ogr_source method
        from src.database.models import MapLayer

        async with get_async_read_connection() as conn:
            layer_row = await conn.fetchrow(
                """
                SELECT *
                FROM map_layers
                WHERE layer_id = $1
                """,
                layer_id,
            )
            if not layer_row:
                markdown_content.append("Error: Layer not found")
                return markdown_content
            layer = MapLayer(**dict(layer_row))

        if _is_geoparquet_backed_vector(layer_data, metadata):
            markdown_content.extend(
                self.describe_vector_layer_from_metadata(layer_data, metadata)
            )
            return markdown_content

        try:
            async with await layer.get_ogr_source() as ogr_source:
                with fiona.open(ogr_source) as src:
                    if layer_data["feature_count"]:
                        feature_count = layer_data["feature_count"]
                    else:
                        try:
                            feature_count = len(src)
                        except TypeError:
                            # Some layer types don't support counting
                            feature_count = None
                    schema = src.schema
                    crs = src.crs

                    markdown_content.append("\n## Geographic Extent\n")
                    if layer_data["bounds"]:
                        markdown_content.append(
                            f"Dataset Bounds: {layer_data['bounds'][0]:.6f},{layer_data['bounds'][1]:.6f},{layer_data['bounds'][2]:.6f},{layer_data['bounds'][3]:.6f}"
                        )

                    markdown_content.append("\n## Schema Information\n")

                    # Update database with feature count if it wasn't already set and we successfully got a count
                    if layer_data["feature_count"] is None and feature_count is not None:
                        async with get_async_db_connection() as conn:
                            await conn.execute(
                                """
                                UPDATE map_layers
                                SET feature_count = $1
                                WHERE layer_id = $2
                                """,
                                feature_count,
                                layer_id,
                            )

                    markdown_content.append(f"CRS: {crs.to_string() if crs else 'Unknown'}")
                    markdown_content.append(f"Driver: {src.driver}")
                    markdown_content.append("\n### Attribute Fields\n")

                    if "properties" in schema and schema["properties"]:
                        fields_by_type = {}
                        for field_name, field_type in schema["properties"].items():
                            if field_type not in fields_by_type:
                                fields_by_type[field_type] = []
                            fields_by_type[field_type].append(field_name)

                        for field_type in sorted(fields_by_type.keys()):
                            markdown_content.append(f"\n#### {field_type}\n")
                            for field_name in sorted(fields_by_type[field_type]):
                                markdown_content.append(f"{field_name}")
                    else:
                        markdown_content.append("No attribute fields found.")

                    features_with_attrs = []
                    for i, feature in enumerate(src):
                        if i >= 10:
                            break
                        features_with_attrs.append(feature["properties"])

                    if features_with_attrs:
                        all_fieldnames = set()
                        for feature_props in features_with_attrs:
                            all_fieldnames.update(feature_props.keys())

                        fieldnames = sorted(list(all_fieldnames))

                        markdown_content.append("\n## Sampled Features Attribute Table\n")

                        if feature_count is not None:
                            markdown_content.append(
                                f"\nRandomly sampled {len(features_with_attrs)} of {feature_count} features for this table."
                            )
                        else:
                            markdown_content.append(
                                f"\nRandomly sampled {len(features_with_attrs)} features for this table."
                            )

                        csv_output = io.StringIO()
                        writer = csv.DictWriter(csv_output, fieldnames=fieldnames)
                        writer.writeheader()

                        for feature_props in features_with_attrs:
                            filtered_props = {}
                            for k in fieldnames:
                                value = feature_props.get(k, "")
                                filtered_props[k] = value
                            writer.writerow(filtered_props)

                        markdown_content.append("```csv")
                        markdown_content.append(csv_output.getvalue())
                        markdown_content.append("```")
        except Exception as exc:
            logger.warning(
                "Vector layer %s fell back to metadata description after OGR read failed: %s",
                layer_id,
                exc,
                exc_info=True,
            )
            markdown_content.extend(
                self.describe_vector_layer_from_metadata(
                    layer_data,
                    metadata,
                    fallback_reason=f"{type(exc).__name__}: {exc}",
                )
            )
            return markdown_content

        return markdown_content


@lru_cache(maxsize=1)
def get_layer_describer() -> LayerDescriber:
    return DefaultLayerDescriber()
