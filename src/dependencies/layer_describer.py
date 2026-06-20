import csv
import asyncio
import io
import json
import logging
import os
import time
from abc import ABC, abstractmethod
from functools import lru_cache
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

from src.structures import get_async_db_connection, get_async_read_connection

GEOPARQUET_DESCRIBE_MAX_BYTES = int(
    os.environ.get("GEOPARQUET_DESCRIBE_MAX_BYTES", str(128 * 1024 * 1024))
)


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


def _geoparquet_key_from_layer(
    layer_data: Dict[str, Any], metadata: Dict[str, Any]
) -> Optional[str]:
    storage_candidates = [
        metadata.get("geoparquet_key"),
        layer_data.get("s3_key"),
        metadata.get("analytics_key"),
        metadata.get("analytics_store_key"),
    ]
    for candidate in storage_candidates:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        lowered = candidate.lower()
        if "geoparquet/" in lowered or lowered.endswith((".parquet", ".geoparquet")):
            return candidate
    return None


def _safe_csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()[:96]
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, default=str)[:240]
    return str(value)[:240]


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _quoted_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _decode_geo_metadata(parquet_metadata: Any) -> Dict[str, Any]:
    raw_metadata = getattr(parquet_metadata, "metadata", None) or {}
    raw_geo = raw_metadata.get(b"geo") or raw_metadata.get("geo")
    if not raw_geo:
        return {}
    if isinstance(raw_geo, bytes):
        raw_geo = raw_geo.decode("utf-8")
    try:
        parsed = json.loads(raw_geo)
    except (TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _short_crs(crs: Any) -> Optional[str]:
    if not crs:
        return None
    if isinstance(crs, str):
        return crs[:160]
    if isinstance(crs, dict):
        crs_id = crs.get("id")
        if isinstance(crs_id, dict):
            authority = crs_id.get("authority")
            code = crs_id.get("code")
            if authority and code:
                return f"{authority}:{code}"
        name = crs.get("name")
        if isinstance(name, str):
            return name[:160]
    return str(type(crs).__name__)


def _geo_column_details(
    geo_metadata: Dict[str, Any], schema_names: list[str]
) -> tuple[Optional[str], Dict[str, Any]]:
    primary_column = geo_metadata.get("primary_column")
    columns = geo_metadata.get("columns")
    if not isinstance(columns, dict):
        columns = {}
    if isinstance(primary_column, str) and primary_column:
        return primary_column, columns.get(primary_column, {})
    if columns:
        first_column = next(iter(columns.keys()))
        return first_column, columns.get(first_column, {})
    if "geometry" in schema_names:
        return "geometry", {}
    return None, {}


def _sample_geoparquet_attributes(
    analytics_path: str,
    attribute_fields: list[str],
    *,
    max_rows: int = 5,
) -> tuple[list[str], list[tuple[Any, ...]]]:
    if not attribute_fields:
        return [], []

    import duckdb

    selected_fields = attribute_fields[:8]
    columns_sql = ", ".join(_quoted_identifier(field) for field in selected_fields)
    query = (
        f"SELECT {columns_sql} "
        f"FROM read_parquet({_sql_literal(analytics_path)}) "
        f"LIMIT {int(max_rows)}"
    )
    con = duckdb.connect(":memory:")
    try:
        cursor = con.execute(query)
        rows = cursor.fetchall()
        headers = [column[0] for column in cursor.description]
        return headers, rows
    finally:
        con.close()


def _describe_geoparquet_file(
    analytics_path: str,
    layer_data: Dict[str, Any],
    metadata: Dict[str, Any],
) -> List[str]:
    import pyarrow.parquet as pq

    parquet_file = pq.ParquetFile(analytics_path)
    file_metadata = parquet_file.metadata
    schema = parquet_file.schema_arrow
    schema_names = list(schema.names)
    geo_metadata = _decode_geo_metadata(file_metadata)
    geo_column, geo_column_metadata = _geo_column_details(geo_metadata, schema_names)

    markdown_content: List[str] = []

    markdown_content.append("\n## Geographic Extent\n")
    formatted_bounds = _format_bounds(layer_data.get("bounds"))
    if formatted_bounds:
        markdown_content.append(f"Dataset Bounds: {formatted_bounds}")
    else:
        geo_bbox = geo_column_metadata.get("bbox")
        formatted_geo_bbox = _format_bounds(geo_bbox)
        markdown_content.append(
            f"Dataset Bounds: {formatted_geo_bbox}" if formatted_geo_bbox else "Dataset Bounds: Unknown"
        )

    markdown_content.append("\n## Schema Information\n")
    markdown_content.append("Driver: GeoParquet")
    markdown_content.append("GeoParquet Reader: PyArrow")
    markdown_content.append("Query Engine: DuckDB read_parquet")
    markdown_content.append("Analytics Store: GeoParquet")
    if metadata.get("pmtiles_key"):
        markdown_content.append("Browser Transport: PMTiles")
    if metadata.get("pmtiles_maxzoom") is not None:
        markdown_content.append(f"PMTiles Max Zoom: {metadata['pmtiles_maxzoom']}")
    if metadata.get("geoparquet_key"):
        markdown_content.append(f"GeoParquet Key: {metadata['geoparquet_key']}")
    if file_metadata:
        markdown_content.append(f"Row Count: {file_metadata.num_rows}")
        markdown_content.append(f"Row Groups: {file_metadata.num_row_groups}")
    if geo_column:
        markdown_content.append(f"Geo Column: {geo_column}")
    encoding = geo_column_metadata.get("encoding")
    if encoding:
        markdown_content.append(f"Geometry Encoding: {encoding}")
    geometry_types = geo_column_metadata.get("geometry_types")
    if geometry_types:
        markdown_content.append(
            f"Geometry Types: {', '.join(map(str, geometry_types))}"
        )
    crs = _short_crs(geo_column_metadata.get("crs"))
    if crs:
        markdown_content.append(f"CRS: {crs}")
    if metadata.get("original_srid"):
        markdown_content.append(f"Original SRID: EPSG:{metadata['original_srid']}")

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
    attribute_fields: list[str] = []
    for field in schema:
        field_type = str(field.type)
        markdown_content.append(f"{field.name}: {field_type}")
        if field.name != geo_column:
            attribute_fields.append(field.name)

    try:
        headers, rows = _sample_geoparquet_attributes(analytics_path, attribute_fields)
    except Exception:
        logger.debug("DuckDB GeoParquet attribute sample failed", exc_info=True)
        headers, rows = [], []

    if headers and rows:
        markdown_content.append("\n## Sampled Features Attribute Table\n")
        markdown_content.append(
            f"\nSampled {len(rows)} of {file_metadata.num_rows} features with DuckDB read_parquet."
        )
        csv_output = io.StringIO()
        writer = csv.DictWriter(csv_output, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    header: _safe_csv_value(value)
                    for header, value in zip(headers, row)
                }
            )
        markdown_content.append("```csv")
        markdown_content.append(csv_output.getvalue())
        markdown_content.append("```")

    return markdown_content


def _describe_ogr_vector_file(
    ogr_source: str,
    layer_data: Dict[str, Any],
) -> tuple[List[str], Optional[int]]:
    import pyogrio

    info = pyogrio.read_info(ogr_source)
    feature_count = layer_data.get("feature_count") or info.get("features")
    bounds = layer_data.get("bounds") or info.get("total_bounds")
    geometry_type = info.get("geometry_type") or layer_data.get("geometry_type")
    fields = list(info.get("fields") or [])
    dtypes = list(info.get("dtypes") or [])

    markdown_content: List[str] = []

    markdown_content.append("\n## Geographic Extent\n")
    formatted_bounds = _format_bounds(bounds)
    markdown_content.append(
        f"Dataset Bounds: {formatted_bounds}" if formatted_bounds else "Dataset Bounds: Unknown"
    )

    markdown_content.append("\n## Schema Information\n")
    crs = info.get("crs")
    markdown_content.append(f"CRS: {crs if crs else 'Unknown'}")
    markdown_content.append("Driver: pyogrio/GDAL")
    if geometry_type:
        markdown_content.append(f"Detected Geometry Type: {str(geometry_type).lower()}")
    if feature_count is not None:
        markdown_content.append(f"Feature Count: {feature_count}")

    markdown_content.append("\n### Attribute Fields\n")
    if fields:
        for field_name, field_type in zip(fields, dtypes):
            markdown_content.append(f"{field_name}: {field_type}")
    else:
        markdown_content.append("No attribute fields found.")

    try:
        sample = pyogrio.read_dataframe(
            ogr_source,
            read_geometry=False,
            max_features=10,
        )
    except Exception:
        logger.debug("pyogrio attribute sample failed", exc_info=True)
        sample = None

    if sample is not None and not sample.empty:
        markdown_content.append("\n## Sampled Features Attribute Table\n")
        if feature_count is not None:
            markdown_content.append(
                f"\nSampled {len(sample)} of {feature_count} features for this table."
            )
        else:
            markdown_content.append(
                f"\nSampled {len(sample)} features for this table."
            )

        csv_output = io.StringIO()
        sample = sample.head(10)
        writer = csv.DictWriter(csv_output, fieldnames=list(sample.columns))
        writer.writeheader()
        for record in sample.to_dict(orient="records"):
            writer.writerow(
                {
                    key: _safe_csv_value(value)
                    for key, value in record.items()
                }
            )

        markdown_content.append("```csv")
        markdown_content.append(csv_output.getvalue())
        markdown_content.append("```")

    return markdown_content, int(feature_count) if feature_count is not None else None


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
            geoparquet_key = _geoparquet_key_from_layer(layer_data, metadata)
            raw_size = metadata.get("geoparquet_size_bytes")
            try:
                geoparquet_size = int(raw_size) if raw_size is not None else None
            except (TypeError, ValueError):
                geoparquet_size = None

            if geoparquet_key and (
                geoparquet_size is None
                or geoparquet_size <= GEOPARQUET_DESCRIBE_MAX_BYTES
            ):
                try:
                    from src.duckdb import _geoparquet_layer_filename

                    async with _geoparquet_layer_filename(layer_id, geoparquet_key) as path:
                        markdown_content.extend(
                            await asyncio.to_thread(
                                _describe_geoparquet_file,
                                path,
                                layer_data,
                                metadata,
                            )
                        )
                    return markdown_content
                except Exception as exc:
                    logger.warning(
                        "GeoParquet layer %s fell back to metadata description after PyArrow/DuckDB read failed: %s",
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

            fallback_reason = (
                "missing GeoParquet key"
                if not geoparquet_key
                else f"GeoParquet file is larger than describe cap ({geoparquet_size} bytes)"
            )
            markdown_content.extend(
                self.describe_vector_layer_from_metadata(
                    layer_data, metadata, fallback_reason=fallback_reason
                )
            )
            return markdown_content

        try:
            async with await layer.get_ogr_source() as ogr_source:
                vector_content, feature_count = await asyncio.to_thread(
                    _describe_ogr_vector_file,
                    ogr_source,
                    layer_data,
                )
                markdown_content.extend(vector_content)

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
