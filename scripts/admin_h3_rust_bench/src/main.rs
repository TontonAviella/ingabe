use geo::{BooleanOps, ChamberlainDuquetteArea, Coord, LineString, MultiPolygon, Polygon};
use h3o::{
    geom::{ContainmentMode, TilerBuilder},
    CellIndex, Resolution,
};
use std::env;
use std::hint::black_box;
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
    runs: usize,
    warmups: usize,
}

#[derive(Clone, Copy)]
struct Sample {
    median_ms: f64,
    p95_ms: f64,
}

#[derive(Clone, Copy)]
struct OverlapSummary {
    cells: usize,
    kept: usize,
    total_overlap: f64,
    weighted_rain: f64,
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
    println!("Rust admin H3 overlap benchmark");
    println!("engine=h3o+geo");
    println!("runs={} warmups={}", config.runs, config.warmups);
    println!();

    for case in CASES {
        let polygon = make_admin_polygon(case);
        let resolution = Resolution::try_from(case.resolution).expect("valid H3 resolution");

        println!(
            "{} level={} res={} input_vertices={}",
            case.name, case.admin_level, case.resolution, case.vertices
        );
        run_case(
            "rust_h3_centroid_ids_only",
            case,
            &polygon,
            resolution,
            ContainmentMode::ContainsCentroid,
            false,
            config,
        );
        run_case(
            "rust_overlap_centroid_candidates",
            case,
            &polygon,
            resolution,
            ContainmentMode::ContainsCentroid,
            true,
            config,
        );
        run_case(
            "rust_h3_intersects_ids_only",
            case,
            &polygon,
            resolution,
            ContainmentMode::IntersectsBoundary,
            false,
            config,
        );
        run_case(
            "rust_overlap_intersects_candidates",
            case,
            &polygon,
            resolution,
            ContainmentMode::IntersectsBoundary,
            true,
            config,
        );
        println!();
    }
}

fn run_case(
    name: &str,
    case: Case,
    polygon: &Polygon,
    resolution: Resolution,
    mode: ContainmentMode,
    include_overlap: bool,
    config: Config,
) {
    let sample = measure(config.runs, config.warmups, || {
        let summary = if include_overlap {
            compute_overlap(polygon, resolution, mode)
        } else {
            let cells = h3_cells(polygon, resolution, mode);
            OverlapSummary {
                cells: cells.len(),
                kept: cells.len(),
                total_overlap: 0.0,
                weighted_rain: 0.0,
            }
        };
        black_box(summary.weighted_rain);
        summary
    });
    let detail = if include_overlap {
        compute_overlap(polygon, resolution, mode)
    } else {
        let cells = h3_cells(polygon, resolution, mode);
        OverlapSummary {
            cells: cells.len(),
            kept: cells.len(),
            total_overlap: 0.0,
            weighted_rain: 0.0,
        }
    };
    println!(
        "  {name:36} median={:9.4}ms p95={:9.4}ms cells={} kept={} total_overlap={:.4}",
        sample.median_ms, sample.p95_ms, detail.cells, detail.kept, detail.total_overlap
    );
    black_box(case);
}

fn parse_args() -> Config {
    let mut runs = 25;
    let mut warmups = 5;
    let args = env::args().skip(1).collect::<Vec<_>>();
    let mut idx = 0;
    while idx < args.len() {
        match args[idx].as_str() {
            "--runs" => {
                idx += 1;
                runs = args
                    .get(idx)
                    .expect("--runs requires a value")
                    .parse()
                    .expect("runs must be an integer");
            }
            "--warmups" => {
                idx += 1;
                warmups = args
                    .get(idx)
                    .expect("--warmups requires a value")
                    .parse()
                    .expect("warmups must be an integer");
            }
            value => panic!("unknown argument: {}", value),
        }
        idx += 1;
    }
    Config { runs, warmups }
}

fn measure<F>(runs: usize, warmups: usize, mut f: F) -> Sample
where
    F: FnMut() -> OverlapSummary,
{
    for _ in 0..warmups {
        f();
    }
    let mut times = Vec::with_capacity(runs);
    for _ in 0..runs {
        let start = Instant::now();
        let summary = f();
        black_box(summary.weighted_rain);
        times.push(start.elapsed().as_secs_f64() * 1000.0);
    }
    times.sort_by(|a, b| a.total_cmp(b));
    Sample {
        median_ms: percentile_sorted(&times, 0.5),
        p95_ms: percentile_sorted(&times, 0.95),
    }
}

fn percentile_sorted(values: &[f64], p: f64) -> f64 {
    if values.is_empty() {
        return 0.0;
    }
    let max_idx = values.len() - 1;
    let rank = (max_idx as f64 * p).round() as usize;
    values[rank.min(max_idx)]
}

fn h3_cells(polygon: &Polygon, resolution: Resolution, mode: ContainmentMode) -> Vec<CellIndex> {
    let mut tiler = TilerBuilder::new(resolution).containment_mode(mode).build();
    tiler.add(polygon.clone()).expect("valid polygon");
    let mut cells = tiler.into_coverage().collect::<Vec<_>>();
    cells.sort_unstable();
    cells
}

fn compute_overlap(
    polygon: &Polygon,
    resolution: Resolution,
    mode: ContainmentMode,
) -> OverlapSummary {
    let admin = MultiPolygon::new(vec![polygon.clone()]);
    let cells = h3_cells(polygon, resolution, mode);
    let mut kept = 0;
    let mut total_overlap = 0.0;
    let mut weighted_rain = 0.0;

    for (idx, cell) in cells.iter().copied().enumerate() {
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
        let rain_value = 20.0 + ((idx * 37 + usize::from(case_len(cell))) % 90) as f64;
        kept += 1;
        total_overlap += overlap_ratio;
        weighted_rain += rain_value * overlap_ratio;
    }

    OverlapSummary {
        cells: cells.len(),
        kept,
        total_overlap,
        weighted_rain,
    }
}

fn case_len(cell: CellIndex) -> u8 {
    cell.to_string().len() as u8
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
