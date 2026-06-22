use geo::{
    BooleanOps, ChamberlainDuquetteArea, Coord, CoordsIter, LineString, MultiPolygon, Polygon,
};
use h3o::{
    geom::{ContainmentMode, TilerBuilder},
    CellIndex, Resolution,
};
use serde_json::json;
use std::io::{self, BufRead, Write};
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

#[derive(Clone)]
struct OverlapCell {
    cell: CellIndex,
    overlap_ratio: f64,
    height: f64,
}

#[derive(Default)]
struct Mesh {
    positions: Vec<f32>,
    normals: Vec<f32>,
    uvs: Vec<f32>,
    indices: Vec<u32>,
}

impl Mesh {
    fn with_capacity(feature_count: usize) -> Self {
        Self {
            positions: Vec::with_capacity(feature_count * 36 * 3),
            normals: Vec::with_capacity(feature_count * 36 * 3),
            uvs: Vec::with_capacity(feature_count * 36 * 2),
            indices: Vec::with_capacity(feature_count * 60),
        }
    }

    fn vertex_count(&self) -> usize {
        self.positions.len() / 3
    }

    fn triangle_count(&self) -> usize {
        self.indices.len() / 3
    }

    fn byte_len(&self) -> usize {
        (self.positions.len() + self.normals.len() + self.uvs.len()) * std::mem::size_of::<f32>()
            + self.indices.len() * std::mem::size_of::<u32>()
    }
}

struct MeshResult {
    case: Case,
    mesh: Mesh,
    features: usize,
    cells: usize,
    total_overlap: f64,
    compute_ms: f64,
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

fn main() -> io::Result<()> {
    let stdin = io::stdin();
    let mut stdout = io::stdout().lock();

    for line in stdin.lock().lines() {
        let line = line?;
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        if line == "quit" {
            break;
        }
        match handle_command(line) {
            Ok(result) => write_mesh_result(&mut stdout, result)?,
            Err(error) => {
                let header = json!({"ok": false, "error": error});
                writeln!(stdout, "{header}")?;
                stdout.flush()?;
            }
        }
    }

    Ok(())
}

fn handle_command(line: &str) -> Result<MeshResult, String> {
    let parts = line.split_whitespace().collect::<Vec<_>>();
    if parts.len() < 3 || parts[0] != "mesh" {
        return Err("expected: mesh <case> <intersects|centroid> [height_scale]".to_string());
    }
    let case = *CASES
        .iter()
        .find(|case| case.name == parts[1])
        .ok_or_else(|| format!("unknown case: {}", parts[1]))?;
    let mode = match parts[2] {
        "intersects" => ContainmentMode::IntersectsBoundary,
        "centroid" => ContainmentMode::ContainsCentroid,
        value => return Err(format!("unknown mode: {value}")),
    };
    let height_scale = if parts.len() > 3 {
        parts[3]
            .parse::<f64>()
            .map_err(|_| format!("invalid height scale: {}", parts[3]))?
    } else {
        100.0
    };
    Ok(build_mesh_result(case, mode, height_scale))
}

fn build_mesh_result(case: Case, mode: ContainmentMode, height_scale: f64) -> MeshResult {
    let started = Instant::now();
    let polygon = make_admin_polygon(case);
    let resolution = Resolution::try_from(case.resolution).expect("valid H3 resolution");
    let overlap_cells = compute_overlap(&polygon, resolution, mode, height_scale);
    let mut mesh = Mesh::with_capacity(overlap_cells.len());
    let mut feature_count = 0;
    let mut total_overlap = 0.0;

    for overlap in &overlap_cells {
        let before = mesh.vertex_count();
        push_h3_prism(&mut mesh, overlap.cell, overlap.height as f32);
        if mesh.vertex_count() > before {
            feature_count += 1;
            total_overlap += overlap.overlap_ratio;
        }
    }

    MeshResult {
        case,
        mesh,
        features: feature_count,
        cells: overlap_cells.len(),
        total_overlap,
        compute_ms: started.elapsed().as_secs_f64() * 1000.0,
    }
}

fn write_mesh_result<W: Write>(writer: &mut W, result: MeshResult) -> io::Result<()> {
    let header = json!({
        "ok": true,
        "engine": "h3o+geo+rust-mesh-sidecar",
        "admin_level": result.case.admin_level,
        "features": result.features,
        "cells": result.cells,
        "vertices": result.mesh.vertex_count(),
        "triangles": result.mesh.triangle_count(),
        "positions_f32": result.mesh.positions.len(),
        "normals_f32": result.mesh.normals.len(),
        "uvs_f32": result.mesh.uvs.len(),
        "indices_u32": result.mesh.indices.len(),
        "mesh_bytes": result.mesh.byte_len(),
        "total_overlap_ratio": round6(result.total_overlap),
        "rust_compute_ms": round6(result.compute_ms),
    });
    writeln!(writer, "{header}")?;
    write_f32_slice(writer, &result.mesh.positions)?;
    write_f32_slice(writer, &result.mesh.normals)?;
    write_f32_slice(writer, &result.mesh.uvs)?;
    write_u32_slice(writer, &result.mesh.indices)?;
    writer.flush()
}

fn write_f32_slice<W: Write>(writer: &mut W, values: &[f32]) -> io::Result<()> {
    for value in values {
        writer.write_all(&value.to_le_bytes())?;
    }
    Ok(())
}

fn write_u32_slice<W: Write>(writer: &mut W, values: &[u32]) -> io::Result<()> {
    for value in values {
        writer.write_all(&value.to_le_bytes())?;
    }
    Ok(())
}

fn compute_overlap(
    polygon: &Polygon,
    resolution: Resolution,
    mode: ContainmentMode,
    height_scale: f64,
) -> Vec<OverlapCell> {
    let admin = MultiPolygon::new(vec![polygon.clone()]);
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
        output.push(OverlapCell {
            cell,
            overlap_ratio,
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

fn push_h3_prism(mesh: &mut Mesh, cell: CellIndex, height: f32) {
    let hex: MultiPolygon = cell.into();
    for polygon in &hex.0 {
        push_polygon_prism(mesh, polygon, height);
    }
}

fn push_polygon_prism(mesh: &mut Mesh, polygon: &Polygon, height: f32) {
    let mut ring = polygon
        .exterior()
        .coords_iter()
        .map(|coord| [coord.x as f32, coord.y as f32])
        .collect::<Vec<_>>();
    if ring.len() < 4 {
        return;
    }
    if ring.first() == ring.last() {
        ring.pop();
    }
    if ring.len() < 3 {
        return;
    }

    let (center_x, center_y) = ring
        .iter()
        .fold((0.0_f32, 0.0_f32), |(x_sum, y_sum), coord| {
            (x_sum + coord[0], y_sum + coord[1])
        });
    let center = [center_x / ring.len() as f32, center_y / ring.len() as f32];

    push_fan_face(mesh, center, &ring, height, true);
    push_fan_face(mesh, center, &ring, 0.0, false);
    for idx in 0..ring.len() {
        let next = (idx + 1) % ring.len();
        push_side_face(mesh, ring[idx], ring[next], height);
    }
}

fn push_fan_face(mesh: &mut Mesh, center: [f32; 2], ring: &[[f32; 2]], z: f32, top: bool) {
    let center_index = push_vertex(
        mesh,
        [center[0], center[1], z],
        if top {
            [0.0, 0.0, 1.0]
        } else {
            [0.0, 0.0, -1.0]
        },
        [0.5, 0.5],
    );
    let mut ring_indices = Vec::with_capacity(ring.len());
    for coord in ring {
        ring_indices.push(push_vertex(
            mesh,
            [coord[0], coord[1], z],
            if top {
                [0.0, 0.0, 1.0]
            } else {
                [0.0, 0.0, -1.0]
            },
            [coord[0], coord[1]],
        ));
    }
    for idx in 0..ring_indices.len() {
        let next = (idx + 1) % ring_indices.len();
        if top {
            mesh.indices
                .extend_from_slice(&[center_index, ring_indices[idx], ring_indices[next]]);
        } else {
            mesh.indices
                .extend_from_slice(&[center_index, ring_indices[next], ring_indices[idx]]);
        }
    }
}

fn push_side_face(mesh: &mut Mesh, a: [f32; 2], b: [f32; 2], height: f32) {
    let dx = b[0] - a[0];
    let dy = b[1] - a[1];
    let length = (dx * dx + dy * dy).sqrt().max(f32::EPSILON);
    let normal = [dy / length, -dx / length, 0.0];
    let base = push_vertex(mesh, [a[0], a[1], 0.0], normal, [0.0, 0.0]);
    let b0 = push_vertex(mesh, [b[0], b[1], 0.0], normal, [1.0, 0.0]);
    let b1 = push_vertex(mesh, [b[0], b[1], height], normal, [1.0, 1.0]);
    let a1 = push_vertex(mesh, [a[0], a[1], height], normal, [0.0, 1.0]);
    mesh.indices
        .extend_from_slice(&[base, b0, b1, base, b1, a1]);
}

fn push_vertex(mesh: &mut Mesh, position: [f32; 3], normal: [f32; 3], uv: [f32; 2]) -> u32 {
    let index = mesh.vertex_count() as u32;
    mesh.positions.extend_from_slice(&position);
    mesh.normals.extend_from_slice(&normal);
    mesh.uvs.extend_from_slice(&uv);
    index
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

fn round6(value: f64) -> f64 {
    (value * 1_000_000.0).round() / 1_000_000.0
}
