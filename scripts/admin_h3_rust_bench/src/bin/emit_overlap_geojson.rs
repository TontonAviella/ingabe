use geo::{BooleanOps, ChamberlainDuquetteArea, Coord, LineString, MultiPolygon, Polygon};
use h3o::{
    geom::{ContainmentMode, TilerBuilder},
    CellIndex, Resolution,
};
use serde_json::{json, Value};
use std::env;
use std::time::Instant;

#[derive(Clone, Copy)]
struct Case {
    name: &'static str,
    admin_level: &'static str,
    resolution: u8,
    width_deg: f64,
    height_deg: f64,
    vertices: usize,
}

#[derive(Clone, Copy)]
struct Config {
    case_name: &'static str,
    containment_mode: ContainmentMode,
    height_scale: f64,
}

#[derive(Clone)]
struct OverlapCell {
    cell: CellIndex,
    overlap_ratio: f64,
    admin_overlap_ratio: f64,
    intersection_area_m2: f64,
    hex_area_m2: f64,
    height: f64,
}

const CASES: [Case; 4] = [
    Case {
        name: "district_r7",
        admin_level: "district",
        resolution: 7,
        width_deg: 0.34,
        height_deg: 0.24,
        vertices: 96,
    },
    Case {
        name: "sector_r8",
        admin_level: "sector",
        resolution: 8,
        width_deg: 0.11,
        height_deg: 0.08,
        vertices: 72,
    },
    Case {
        name: "admin_cell_r9",
        admin_level: "admin_cell",
        resolution: 9,
        width_deg: 0.038,
        height_deg: 0.028,
        vertices: 48,
    },
    Case {
        name: "village_r10",
        admin_level: "village",
        resolution: 10,
        width_deg: 0.014,
        height_deg: 0.010,
        vertices: 32,
    },
];

fn main() {
    let config = parse_args();
    let case = *CASES
        .iter()
        .find(|case| case.name == config.case_name)
        .unwrap_or_else(|| panic!("unknown case: {}", config.case_name));

    let started = Instant::now();
    let polygon = make_admin_polygon(case);
    let resolution = Resolution::try_from(case.resolution).expect("valid H3 resolution");
    let overlap_cells = compute_overlap(
        &polygon,
        resolution,
        config.containment_mode,
        config.height_scale,
    );
    let compute_ms = started.elapsed().as_secs_f64() * 1000.0;

    let feature_count = overlap_cells.len();
    let total_overlap: f64 = overlap_cells.iter().map(|cell| cell.overlap_ratio).sum();
    let features = overlap_cells
        .into_iter()
        .map(|overlap| {
            json!({
                "type": "Feature",
                "properties": {
                    "id": overlap.cell.to_string(),
                    "h3_index": overlap.cell.to_string(),
                    "h3_resolution": case.resolution,
                    "admin_level": case.admin_level,
                    "admin_id": format!("{}-demo", case.admin_level),
                    "admin_name": format!("Demo {}", case.admin_level),
                    "overlap_ratio": round6(overlap.overlap_ratio),
                    "admin_overlap_ratio": round8(overlap.admin_overlap_ratio),
                    "intersection_area_m2": round3(overlap.intersection_area_m2),
                    "hex_area_m2": round3(overlap.hex_area_m2),
                    "height": round3(overlap.height),
                    "risk_score": round3(overlap.height),
                },
                "geometry": cell_geometry_json(overlap.cell),
            })
        })
        .collect::<Vec<_>>();

    let payload = json!({
        "type": "FeatureCollection",
        "features": features,
        "metadata": {
            "engine": "h3o+geo",
            "case": case.name,
            "admin_level": case.admin_level,
            "h3_resolution": case.resolution,
            "containment_mode": containment_mode_name(config.containment_mode),
            "feature_count": feature_count,
            "input_vertices": case.vertices,
            "total_overlap_ratio": round6(total_overlap),
            "rust_compute_ms": round6(compute_ms),
        },
    });
    println!(
        "{}",
        serde_json::to_string(&payload).expect("serialize GeoJSON payload")
    );
}

fn parse_args() -> Config {
    let mut case_name = "district_r7";
    let mut containment_mode = ContainmentMode::IntersectsBoundary;
    let mut height_scale = 100.0;
    let args = env::args().skip(1).collect::<Vec<_>>();
    let mut idx = 0;

    while idx < args.len() {
        match args[idx].as_str() {
            "--case" => {
                idx += 1;
                let value = args.get(idx).expect("--case requires a value");
                case_name = match value.as_str() {
                    "district_r7" => "district_r7",
                    "sector_r8" => "sector_r8",
                    "admin_cell_r9" => "admin_cell_r9",
                    "village_r10" => "village_r10",
                    other => panic!("unknown case: {}", other),
                };
            }
            "--mode" => {
                idx += 1;
                containment_mode = match args.get(idx).expect("--mode requires a value").as_str() {
                    "centroid" => ContainmentMode::ContainsCentroid,
                    "intersects" => ContainmentMode::IntersectsBoundary,
                    other => panic!("unknown mode: {}", other),
                };
            }
            "--height-scale" => {
                idx += 1;
                height_scale = args
                    .get(idx)
                    .expect("--height-scale requires a value")
                    .parse()
                    .expect("height scale must be a number");
            }
            value => panic!("unknown argument: {}", value),
        }
        idx += 1;
    }

    Config {
        case_name,
        containment_mode,
        height_scale,
    }
}

fn compute_overlap(
    polygon: &Polygon,
    resolution: Resolution,
    mode: ContainmentMode,
    height_scale: f64,
) -> Vec<OverlapCell> {
    let admin = MultiPolygon::new(vec![polygon.clone()]);
    let admin_area_m2 = admin.chamberlain_duquette_unsigned_area();
    let cells = h3_cells(polygon, resolution, mode);
    let mut output = Vec::with_capacity(cells.len());

    for cell in cells {
        let hex: MultiPolygon = cell.into();
        let hex_area_m2 = hex.chamberlain_duquette_unsigned_area();
        if hex_area_m2 <= f64::EPSILON {
            continue;
        }
        let intersection = admin.intersection(&hex);
        let intersection_area_m2 = intersection.chamberlain_duquette_unsigned_area();
        if intersection_area_m2 <= 1e-6 {
            continue;
        }
        let overlap_ratio = intersection_area_m2 / hex_area_m2;
        let admin_overlap_ratio = if admin_area_m2 > f64::EPSILON {
            intersection_area_m2 / admin_area_m2
        } else {
            0.0
        };
        output.push(OverlapCell {
            cell,
            overlap_ratio,
            admin_overlap_ratio,
            intersection_area_m2,
            hex_area_m2,
            height: (overlap_ratio * height_scale).max(0.5),
        });
    }

    output
}

fn h3_cells(polygon: &Polygon, resolution: Resolution, mode: ContainmentMode) -> Vec<CellIndex> {
    let mut tiler = TilerBuilder::new(resolution).containment_mode(mode).build();
    tiler.add(polygon.clone()).expect("valid polygon");
    let mut cells = tiler.into_coverage().collect::<Vec<_>>();
    cells.sort_unstable();
    cells
}

fn cell_geometry_json(cell: CellIndex) -> Value {
    let hex: MultiPolygon = cell.into();
    if hex.0.len() == 1 {
        json!({
            "type": "Polygon",
            "coordinates": polygon_coordinates(&hex.0[0]),
        })
    } else {
        json!({
            "type": "MultiPolygon",
            "coordinates": hex.0.iter().map(polygon_coordinates).collect::<Vec<_>>(),
        })
    }
}

fn polygon_coordinates(polygon: &Polygon) -> Vec<Vec<Vec<f64>>> {
    let mut rings = vec![linestring_coordinates(polygon.exterior())];
    rings.extend(polygon.interiors().iter().map(linestring_coordinates));
    rings
}

fn linestring_coordinates(line: &LineString) -> Vec<Vec<f64>> {
    let mut coords = line
        .coords()
        .map(|coord| vec![coord.x, coord.y])
        .collect::<Vec<_>>();
    if coords.first() != coords.last() {
        if let Some(first) = coords.first().cloned() {
            coords.push(first);
        }
    }
    coords
}

fn make_admin_polygon(case: Case) -> Polygon {
    let center_lng = 30.05;
    let center_lat = -1.95;
    let rx = case.width_deg / 2.0;
    let ry = case.height_deg / 2.0;
    let mut coords = Vec::with_capacity(case.vertices + 1);
    for idx in 0..case.vertices {
        let angle = (2.0 * std::f64::consts::PI * idx as f64) / case.vertices as f64;
        let wobble = 1.0 + 0.11 * (angle * 3.0).sin() + 0.06 * (angle * 7.0).cos();
        coords.push(Coord {
            x: center_lng + angle.cos() * rx * wobble,
            y: center_lat + angle.sin() * ry * wobble,
        });
    }
    coords.push(coords[0]);
    Polygon::new(LineString::new(coords), vec![])
}

fn containment_mode_name(mode: ContainmentMode) -> &'static str {
    match mode {
        ContainmentMode::ContainsCentroid => "centroid",
        ContainmentMode::ContainsBoundary => "contains_boundary",
        ContainmentMode::IntersectsBoundary => "intersects",
        ContainmentMode::Covers => "covers",
        _ => "unknown",
    }
}

fn round3(value: f64) -> f64 {
    (value * 1_000.0).round() / 1_000.0
}

fn round6(value: f64) -> f64 {
    (value * 1_000_000.0).round() / 1_000_000.0
}

fn round8(value: f64) -> f64 {
    (value * 100_000_000.0).round() / 100_000_000.0
}
