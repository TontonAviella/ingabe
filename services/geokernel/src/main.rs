use std::collections::BTreeSet;
use std::net::SocketAddr;
use std::time::Instant;

use anyhow::Result;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use geo::{
    BooleanOps, Centroid, ChamberlainDuquetteArea, Contains, Coord, LineString, MultiPolygon,
    Polygon,
};
use h3o::{
    geom::{ContainmentMode, TilerBuilder},
    CellIndex, Resolution,
};
use serde::Deserialize;
use serde_json::{json, Map, Value};
use tower_http::trace::TraceLayer;
use tracing::info;

const DEFAULT_MAX_HEXES: usize = 50_000;

#[derive(Debug, Deserialize)]
struct AdminH3OverlapRequest {
    geojson: Value,
    resolution: u8,
    admin_level: Option<String>,
    id_property: Option<String>,
    name_property: Option<String>,
    max_hexes: Option<usize>,
    min_overlap_ratio: Option<f64>,
    include_geometry: Option<bool>,
    containment_mode: Option<String>,
}

struct InputFeature {
    source_feature_index: usize,
    geometry: MultiPolygon<f64>,
    properties: Map<String, Value>,
}

#[derive(Debug)]
struct ApiError {
    status: StatusCode,
    message: String,
}

impl ApiError {
    fn bad_request(message: impl Into<String>) -> Self {
        Self {
            status: StatusCode::BAD_REQUEST,
            message: message.into(),
        }
    }

    fn internal(message: impl Into<String>) -> Self {
        Self {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            message: message.into(),
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (
            self.status,
            Json(json!({
                "status": "error",
                "error": self.message,
            })),
        )
            .into_response()
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            std::env::var("RUST_LOG")
                .unwrap_or_else(|_| "mundi_geokernel=info,tower_http=warn".to_string()),
        )
        .init();

    let app = Router::new()
        .route("/healthz", get(healthz))
        .route("/admin/h3-overlap", post(admin_h3_overlap))
        .layer(TraceLayer::new_for_http());

    let port = std::env::var("GEOKERNEL_PORT")
        .ok()
        .and_then(|s| s.parse::<u16>().ok())
        .unwrap_or(8878);
    let addr = SocketAddr::from(([0, 0, 0, 0], port));
    info!("mundi-geokernel listening on {addr}");

    let listener = tokio::net::TcpListener::bind(addr).await?;
    axum::serve(listener, app).await?;
    Ok(())
}

async fn healthz() -> Json<Value> {
    Json(json!({
        "status": "ok",
        "engine": "mundi-geokernel",
        "capabilities": [
            "admin_h3_overlap",
            "h3o_coverage",
            "geo_boolean_overlap"
        ],
        "geometry_engine": "geo",
        "robust_kernel_available": "geo::algorithm::kernels::RobustKernel",
    }))
}

async fn admin_h3_overlap(
    Json(request): Json<AdminH3OverlapRequest>,
) -> Result<Json<Value>, ApiError> {
    let started = Instant::now();
    let response = build_admin_h3_overlap_response(request, started)?;
    Ok(Json(response))
}

fn build_admin_h3_overlap_response(
    request: AdminH3OverlapRequest,
    started: Instant,
) -> Result<Value, ApiError> {
    validate_request(&request)?;
    let resolution = Resolution::try_from(request.resolution)
        .map_err(|_| ApiError::bad_request("H3 resolution must be between 0 and 15."))?;
    let containment_mode = parse_containment_mode(request.containment_mode.as_deref())?;
    let max_hexes = request.max_hexes.unwrap_or(DEFAULT_MAX_HEXES);
    let min_overlap_ratio = request.min_overlap_ratio.unwrap_or(0.0);
    let include_geometry = request.include_geometry.unwrap_or(true);
    let input_features = iter_features(&request.geojson)?;

    let mut output_features = Vec::new();
    let mut total_candidates = 0usize;

    for feature in input_features {
        let cells = h3_cells(&feature.geometry, resolution, containment_mode)?;
        total_candidates += cells.len();
        if total_candidates > max_hexes {
            return Err(ApiError::bad_request(format!(
                "Generated {total_candidates} H3 cells, above limit {max_hexes}. Use a coarser resolution or smaller admin boundary."
            )));
        }

        let admin_area_m2 = feature.geometry.chamberlain_duquette_unsigned_area();
        let admin_id = admin_id(
            &feature.properties,
            request.id_property.as_deref(),
            request.admin_level.as_deref(),
            feature.source_feature_index,
        );
        let admin_name = admin_name(
            &feature.properties,
            request.name_property.as_deref(),
            request.admin_level.as_deref(),
            &admin_id,
        );

        for cell in cells {
            let hex: MultiPolygon<f64> = cell.into();
            let hex_area_m2 = hex.chamberlain_duquette_unsigned_area();
            if hex_area_m2 <= f64::EPSILON {
                continue;
            }

            let intersection = feature.geometry.intersection(&hex);
            let intersection_area_m2 = intersection.chamberlain_duquette_unsigned_area();
            if intersection_area_m2 <= 1e-6 {
                continue;
            }

            let overlap_ratio = intersection_area_m2 / hex_area_m2;
            if overlap_ratio < min_overlap_ratio {
                continue;
            }

            let mut output = json!({
                "type": "Feature",
                "properties": {
                    "h3_index": cell.to_string(),
                    "h3_resolution": request.resolution,
                    "admin_level": request.admin_level,
                    "admin_id": admin_id,
                    "admin_name": admin_name,
                    "source_feature_index": feature.source_feature_index,
                    "overlap_ratio": round_to(overlap_ratio, 6),
                    "admin_overlap_ratio": round_to(ratio(intersection_area_m2, admin_area_m2), 8),
                    "centroid_inside": centroid_inside(&feature.geometry, &hex),
                    "intersection_area_m2": round_to(intersection_area_m2, 3),
                    "hex_area_m2": round_to(hex_area_m2, 3),
                    "admin_area_m2": round_to(admin_area_m2, 3),
                }
            });

            if include_geometry {
                output["geometry"] = cell_geojson_geometry(cell);
            }
            output_features.push(output);
        }
    }

    let compute_ms = started.elapsed().as_secs_f64() * 1000.0;
    Ok(json!({
        "type": "FeatureCollection",
        "features": output_features,
        "metadata": {
            "h3_resolution": request.resolution,
            "admin_level": request.admin_level,
            "feature_count": output_features.len(),
            "max_hexes": max_hexes,
            "min_overlap_ratio": min_overlap_ratio,
            "geometry_included": include_geometry,
            "engine": "mundi-geokernel",
            "h3_engine": "h3o",
            "geometry_engine": "geo",
            "robust_kernel_available": "geo::algorithm::kernels::RobustKernel",
            "containment_mode": containment_mode_name(containment_mode),
            "rust_compute_ms": round_to(compute_ms, 3),
            "candidate_hexes": total_candidates,
        },
    }))
}

fn validate_request(request: &AdminH3OverlapRequest) -> Result<(), ApiError> {
    if request.resolution > 15 {
        return Err(ApiError::bad_request(
            "H3 resolution must be between 0 and 15.",
        ));
    }
    if request.max_hexes.unwrap_or(DEFAULT_MAX_HEXES) < 1 {
        return Err(ApiError::bad_request("max_hexes must be at least 1."));
    }
    let min_overlap_ratio = request.min_overlap_ratio.unwrap_or(0.0);
    if !(0.0..=1.0).contains(&min_overlap_ratio) {
        return Err(ApiError::bad_request(
            "min_overlap_ratio must be between 0 and 1.",
        ));
    }
    Ok(())
}

fn parse_containment_mode(raw: Option<&str>) -> Result<ContainmentMode, ApiError> {
    match raw
        .unwrap_or("centroid")
        .trim()
        .to_ascii_lowercase()
        .as_str()
    {
        "" | "centroid" | "contains_centroid" | "contains-centroid" => {
            Ok(ContainmentMode::ContainsCentroid)
        }
        "intersects" | "intersects_boundary" | "intersects-boundary" => {
            Ok(ContainmentMode::IntersectsBoundary)
        }
        value => Err(ApiError::bad_request(format!(
            "unsupported containment_mode: {value}"
        ))),
    }
}

fn containment_mode_name(mode: ContainmentMode) -> &'static str {
    match mode {
        ContainmentMode::ContainsCentroid => "centroid",
        ContainmentMode::IntersectsBoundary => "intersects",
        _ => "unknown",
    }
}

fn iter_features(geojson: &Value) -> Result<Vec<InputFeature>, ApiError> {
    let object = geojson
        .as_object()
        .ok_or_else(|| ApiError::bad_request("GeoJSON must be an object."))?;
    match object.get("type").and_then(Value::as_str) {
        Some("FeatureCollection") => {
            let features = object
                .get("features")
                .and_then(Value::as_array)
                .ok_or_else(|| {
                    ApiError::bad_request("FeatureCollection must include a features array.")
                })?;
            features
                .iter()
                .enumerate()
                .filter_map(|(idx, feature)| feature.as_object().map(|obj| (idx, obj)))
                .map(|(idx, feature)| feature_from_object(idx, feature))
                .filter_map(Result::transpose)
                .collect()
        }
        Some("Feature") => {
            feature_from_object(0, object).map(|feature| feature.into_iter().collect())
        }
        Some("Polygon") | Some("MultiPolygon") => parse_geometry(geojson)
            .map(|geometry| {
                geometry.map(|geometry| {
                    vec![InputFeature {
                        source_feature_index: 0,
                        geometry,
                        properties: Map::new(),
                    }]
                })
            })
            .map(Option::unwrap_or_default),
        _ => Err(ApiError::bad_request(
            "GeoJSON must be a FeatureCollection, Feature, Polygon, or MultiPolygon.",
        )),
    }
}

fn feature_from_object(
    source_feature_index: usize,
    feature: &Map<String, Value>,
) -> Result<Option<InputFeature>, ApiError> {
    let geometry_value = feature.get("geometry").ok_or_else(|| {
        ApiError::bad_request(format!(
            "Feature {source_feature_index} is missing a GeoJSON geometry."
        ))
    })?;
    let Some(geometry) = parse_geometry(geometry_value)? else {
        return Ok(None);
    };
    let properties = feature
        .get("properties")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    Ok(Some(InputFeature {
        source_feature_index,
        geometry,
        properties,
    }))
}

fn parse_geometry(geometry: &Value) -> Result<Option<MultiPolygon<f64>>, ApiError> {
    let object = geometry
        .as_object()
        .ok_or_else(|| ApiError::bad_request("GeoJSON geometry must be an object."))?;
    match object.get("type").and_then(Value::as_str) {
        Some("Polygon") => {
            let coordinates = object
                .get("coordinates")
                .ok_or_else(|| ApiError::bad_request("Polygon geometry is missing coordinates."))?;
            let polygon = parse_polygon_coordinates(coordinates)?;
            Ok(Some(MultiPolygon::new(vec![polygon])))
        }
        Some("MultiPolygon") => {
            let coordinates = object
                .get("coordinates")
                .and_then(Value::as_array)
                .ok_or_else(|| {
                    ApiError::bad_request("MultiPolygon geometry is missing coordinates.")
                })?;
            let mut polygons = Vec::with_capacity(coordinates.len());
            for polygon in coordinates {
                polygons.push(parse_polygon_coordinates(polygon)?);
            }
            Ok(Some(MultiPolygon::new(polygons)))
        }
        Some(_) => Ok(None),
        None => Err(ApiError::bad_request("GeoJSON geometry is missing type.")),
    }
}

fn parse_polygon_coordinates(value: &Value) -> Result<Polygon<f64>, ApiError> {
    let rings = value
        .as_array()
        .ok_or_else(|| ApiError::bad_request("Polygon coordinates must be an array of rings."))?;
    if rings.is_empty() {
        return Err(ApiError::bad_request(
            "Polygon must include an exterior ring.",
        ));
    }

    let exterior = parse_ring(&rings[0])?;
    if exterior.0.len() < 4 {
        return Err(ApiError::bad_request(
            "Polygon exterior ring must contain at least four positions.",
        ));
    }

    let mut interiors = Vec::with_capacity(rings.len().saturating_sub(1));
    for ring in rings.iter().skip(1) {
        let interior = parse_ring(ring)?;
        if interior.0.len() >= 4 {
            interiors.push(interior);
        }
    }
    Ok(Polygon::new(exterior, interiors))
}

fn parse_ring(value: &Value) -> Result<LineString<f64>, ApiError> {
    let positions = value
        .as_array()
        .ok_or_else(|| ApiError::bad_request("Polygon ring must be an array of positions."))?;
    let mut coords = Vec::with_capacity(positions.len() + 1);
    for position in positions {
        let pair = position
            .as_array()
            .ok_or_else(|| ApiError::bad_request("GeoJSON position must be an array."))?;
        if pair.len() < 2 {
            return Err(ApiError::bad_request(
                "GeoJSON position must contain longitude and latitude.",
            ));
        }
        let x = pair[0]
            .as_f64()
            .ok_or_else(|| ApiError::bad_request("GeoJSON longitude must be numeric."))?;
        let y = pair[1]
            .as_f64()
            .ok_or_else(|| ApiError::bad_request("GeoJSON latitude must be numeric."))?;
        coords.push(Coord { x, y });
    }
    if coords.first() != coords.last() {
        if let Some(first) = coords.first().copied() {
            coords.push(first);
        }
    }
    Ok(LineString::new(coords))
}

fn h3_cells(
    geometry: &MultiPolygon<f64>,
    resolution: Resolution,
    containment_mode: ContainmentMode,
) -> Result<Vec<CellIndex>, ApiError> {
    let mut cells = BTreeSet::new();
    for polygon in &geometry.0 {
        let mut tiler = TilerBuilder::new(resolution)
            .containment_mode(containment_mode)
            .build();
        tiler
            .add(polygon.clone())
            .map_err(|e| ApiError::internal(format!("H3 tiling failed: {e}")))?;
        cells.extend(tiler.into_coverage());
    }
    Ok(cells.into_iter().collect())
}

fn cell_geojson_geometry(cell: CellIndex) -> Value {
    let hex: MultiPolygon<f64> = cell.into();
    if hex.0.len() == 1 {
        polygon_geojson_geometry(&hex.0[0])
    } else {
        json!({
            "type": "MultiPolygon",
            "coordinates": hex.0.iter().map(polygon_coordinates).collect::<Vec<_>>(),
        })
    }
}

fn polygon_geojson_geometry(polygon: &Polygon<f64>) -> Value {
    json!({
        "type": "Polygon",
        "coordinates": polygon_coordinates(polygon),
    })
}

fn polygon_coordinates(polygon: &Polygon<f64>) -> Vec<Vec<Vec<f64>>> {
    let mut rings = Vec::with_capacity(1 + polygon.interiors().len());
    rings.push(linestring_coordinates(polygon.exterior()));
    for ring in polygon.interiors() {
        rings.push(linestring_coordinates(ring));
    }
    rings
}

fn linestring_coordinates(line: &LineString<f64>) -> Vec<Vec<f64>> {
    let mut coords = line
        .points()
        .map(|point| vec![point.x(), point.y()])
        .collect::<Vec<_>>();
    if coords.first() != coords.last() {
        if let Some(first) = coords.first().cloned() {
            coords.push(first);
        }
    }
    coords
}

fn centroid_inside(admin: &MultiPolygon<f64>, hex: &MultiPolygon<f64>) -> bool {
    let Some(centroid) = hex.centroid() else {
        return false;
    };
    admin.contains(&centroid)
}

fn admin_id(
    props: &Map<String, Value>,
    id_property: Option<&str>,
    admin_level: Option<&str>,
    feature_index: usize,
) -> String {
    if let Some(key) = id_property {
        if let Some(value) = props.get(key).and_then(value_to_string) {
            return value;
        }
    }

    let level = admin_level.unwrap_or("").to_ascii_lowercase();
    let mut candidates = vec!["admin_id", "id", "gid", "code"];
    let level_id = format!("{level}_id");
    let level_code = format!("{level}_code");
    if !level.is_empty() {
        candidates.push(&level_id);
        candidates.push(&level_code);
    }
    first_property(props, &candidates).unwrap_or_else(|| format!("admin-{}", feature_index + 1))
}

fn admin_name(
    props: &Map<String, Value>,
    name_property: Option<&str>,
    admin_level: Option<&str>,
    admin_id: &str,
) -> String {
    if let Some(key) = name_property {
        if let Some(value) = props.get(key).and_then(value_to_string) {
            return value;
        }
    }

    let level = admin_level.unwrap_or("").to_ascii_lowercase();
    let mut candidates = vec!["admin_name", "name"];
    let level_name = format!("{level}_name");
    if !level.is_empty() {
        candidates.push(&level_name);
        candidates.push(&level);
    }
    candidates.extend([
        "district",
        "district_name",
        "sector",
        "sector_name",
        "cell",
        "cell_name",
        "village",
        "village_name",
    ]);
    first_property(props, &candidates).unwrap_or_else(|| admin_id.to_string())
}

fn first_property(props: &Map<String, Value>, candidates: &[&str]) -> Option<String> {
    for candidate in candidates {
        if let Some(value) = props.get(*candidate).and_then(value_to_string) {
            return Some(value);
        }
        let candidate_lower = candidate.to_ascii_lowercase();
        for (key, value) in props {
            if key.to_ascii_lowercase() == candidate_lower {
                if let Some(text) = value_to_string(value) {
                    return Some(text);
                }
            }
        }
    }
    None
}

fn value_to_string(value: &Value) -> Option<String> {
    match value {
        Value::Null => None,
        Value::String(text) => Some(text.clone()),
        Value::Bool(flag) => Some(flag.to_string()),
        Value::Number(number) => Some(number.to_string()),
        _ => Some(value.to_string()),
    }
}

fn ratio(numerator: f64, denominator: f64) -> f64 {
    if denominator <= f64::EPSILON {
        0.0
    } else {
        numerator / denominator
    }
}

fn round_to(value: f64, places: i32) -> f64 {
    let factor = 10_f64.powi(places);
    (value * factor).round() / factor
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_request() -> AdminH3OverlapRequest {
        AdminH3OverlapRequest {
            geojson: json!({
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "properties": {
                        "district_id": "D001",
                        "district_name": "Demo District"
                    },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[
                            [30.0, -2.0],
                            [30.02, -2.0],
                            [30.02, -1.98],
                            [30.0, -1.98],
                            [30.0, -2.0]
                        ]]
                    }
                }]
            }),
            resolution: 9,
            admin_level: Some("district".to_string()),
            id_property: Some("district_id".to_string()),
            name_property: Some("district_name".to_string()),
            max_hexes: Some(50_000),
            min_overlap_ratio: Some(0.0),
            include_geometry: Some(true),
            containment_mode: Some("centroid".to_string()),
        }
    }

    #[test]
    fn builds_overlap_feature_collection() {
        let result = build_admin_h3_overlap_response(sample_request(), Instant::now()).unwrap();
        assert_eq!(result["type"], "FeatureCollection");
        assert!(result["features"].as_array().unwrap().len() > 0);
        assert_eq!(result["metadata"]["engine"], "mundi-geokernel");
        assert_eq!(result["metadata"]["h3_engine"], "h3o");
        assert_eq!(result["metadata"]["geometry_engine"], "geo");
        assert_eq!(
            result["metadata"]["robust_kernel_available"],
            "geo::algorithm::kernels::RobustKernel"
        );

        let feature = &result["features"][0];
        assert_eq!(feature["properties"]["admin_id"], "D001");
        assert_eq!(feature["properties"]["admin_name"], "Demo District");
        assert!(
            feature["properties"]["intersection_area_m2"]
                .as_f64()
                .unwrap()
                > 0.0
        );
        assert!(feature.get("geometry").is_some());
    }

    #[test]
    fn can_omit_geometry() {
        let mut request = sample_request();
        request.include_geometry = Some(false);
        let result = build_admin_h3_overlap_response(request, Instant::now()).unwrap();
        let feature = &result["features"][0];
        assert!(feature.get("geometry").is_none());
        assert_eq!(result["metadata"]["geometry_included"], false);
    }

    #[test]
    fn enforces_hex_limit() {
        let mut request = sample_request();
        request.max_hexes = Some(1);
        let error = build_admin_h3_overlap_response(request, Instant::now()).unwrap_err();
        assert_eq!(error.status, StatusCode::BAD_REQUEST);
        assert!(error.message.contains("above limit"));
    }

    #[test]
    fn accepts_intersects_mode() {
        let mut request = sample_request();
        request.containment_mode = Some("intersects".to_string());
        let result = build_admin_h3_overlap_response(request, Instant::now()).unwrap();
        assert_eq!(result["metadata"]["containment_mode"], "intersects");
    }
}
