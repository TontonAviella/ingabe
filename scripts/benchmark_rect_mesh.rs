use std::env;
use std::hint::black_box;
use std::time::Instant;

const BBOX: [f32; 4] = [30.0, -2.05, 30.12, -1.93];

#[derive(Clone, Debug)]
struct Config {
    features: Vec<usize>,
    runs: usize,
    warmups: usize,
}

#[derive(Default)]
struct MeshBuffers {
    positions: Vec<f32>,
    normals: Vec<f32>,
    uvs: Vec<f32>,
    indices: Vec<u32>,
}

impl MeshBuffers {
    fn with_capacity(feature_count: usize) -> Self {
        Self {
            positions: Vec::with_capacity(feature_count * 24 * 3),
            normals: Vec::with_capacity(feature_count * 24 * 3),
            uvs: Vec::with_capacity(feature_count * 24 * 2),
            indices: Vec::with_capacity(feature_count * 36),
        }
    }

    fn clear(&mut self) {
        self.positions.clear();
        self.normals.clear();
        self.uvs.clear();
        self.indices.clear();
    }

    fn bytes(&self) -> usize {
        (self.positions.len() + self.normals.len() + self.uvs.len()) * std::mem::size_of::<f32>()
            + self.indices.len() * std::mem::size_of::<u32>()
    }

    fn vertex_count(&self) -> usize {
        self.positions.len() / 3
    }

    fn triangle_count(&self) -> usize {
        self.indices.len() / 3
    }

    fn checksum(&self) -> f32 {
        let pos = self.positions.iter().step_by(97).copied().sum::<f32>();
        let idx = self
            .indices
            .iter()
            .step_by(89)
            .map(|value| *value as f32)
            .sum::<f32>();
        pos + idx
    }
}

#[derive(Clone, Copy)]
struct Sample {
    median_ms: f64,
    p95_ms: f64,
}

fn main() {
    let config = parse_args();
    println!("Pure Rust rectangle mesh benchmark");
    println!("runs={} warmups={}", config.runs, config.warmups);
    println!();

    for feature_count in config.features {
        let mut reusable = MeshBuffers::with_capacity(feature_count);
        let alloc_sample = measure(config.runs, config.warmups, || {
            let mesh = build_rect_mesh_alloc(feature_count);
            black_box(mesh.checksum());
        });
        let reuse_sample = measure(config.runs, config.warmups, || {
            build_rect_mesh_reuse(feature_count, &mut reusable);
            black_box(reusable.checksum());
        });
        let mesh = build_rect_mesh_alloc(feature_count);

        println!(
            "{feature_count:6} pure_rust_rect_mesh_alloc median={:8.3}ms p95={:8.3}ms bytes={} vertices={} tris={}",
            alloc_sample.median_ms,
            alloc_sample.p95_ms,
            mesh.bytes(),
            mesh.vertex_count(),
            mesh.triangle_count(),
        );
        println!(
            "{feature_count:6} pure_rust_rect_mesh_reuse median={:8.3}ms p95={:8.3}ms bytes={} vertices={} tris={}",
            reuse_sample.median_ms,
            reuse_sample.p95_ms,
            reusable.bytes(),
            reusable.vertex_count(),
            reusable.triangle_count(),
        );
        println!();
    }
}

fn parse_args() -> Config {
    let mut features = vec![16, 256, 1024, 4096];
    let mut runs = 25;
    let mut warmups = 5;
    let args = env::args().skip(1).collect::<Vec<_>>();
    let mut idx = 0;

    while idx < args.len() {
        match args[idx].as_str() {
            "--features" => {
                idx += 1;
                features.clear();
                while idx < args.len() && !args[idx].starts_with("--") {
                    features.push(args[idx].parse().expect("feature count must be an integer"));
                    idx += 1;
                }
                continue;
            }
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

    Config {
        features,
        runs,
        warmups,
    }
}

fn measure<F>(runs: usize, warmups: usize, mut f: F) -> Sample
where
    F: FnMut(),
{
    for _ in 0..warmups {
        f();
    }

    let mut times = Vec::with_capacity(runs);
    for _ in 0..runs {
        let start = Instant::now();
        f();
        times.push(start.elapsed().as_secs_f64() * 1000.0);
    }
    times.sort_by(|a, b| a.total_cmp(b));

    let median = percentile_sorted(&times, 0.5);
    let p95 = percentile_sorted(&times, 0.95);
    Sample {
        median_ms: median,
        p95_ms: p95,
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

fn build_rect_mesh_alloc(feature_count: usize) -> MeshBuffers {
    let mut mesh = MeshBuffers::with_capacity(feature_count);
    build_rect_mesh_reuse(feature_count, &mut mesh);
    mesh
}

fn build_rect_mesh_reuse(feature_count: usize, mesh: &mut MeshBuffers) {
    mesh.clear();

    let side = (feature_count as f32).sqrt().ceil().max(1.0);
    let west = BBOX[0];
    let south = BBOX[1];
    let east = BBOX[2];
    let north = BBOX[3];
    let dx = (east - west) / side;
    let dy = (north - south) / side;

    for feature_idx in 0..feature_count {
        let feature = feature_idx as f32;
        let row = (feature / side).floor();
        let col = feature - row * side;
        let x0 = west + col * dx;
        let y0 = south + row * dy;
        let x1 = x0 + dx * 0.82;
        let y1 = y0 + dy * 0.82;
        let height = 4.0 + ((row * 13.0 + col * 7.0) % 35.0);
        let base_vertex = (feature_idx * 24) as u32;

        push_face(
            mesh,
            base_vertex,
            [0.0, 0.0, -1.0],
            [[x0, y0, 0.0], [x1, y0, 0.0], [x1, y1, 0.0], [x0, y1, 0.0]],
        );
        push_face(
            mesh,
            base_vertex + 4,
            [0.0, 0.0, 1.0],
            [
                [x0, y0, height],
                [x0, y1, height],
                [x1, y1, height],
                [x1, y0, height],
            ],
        );
        push_face(
            mesh,
            base_vertex + 8,
            [0.0, -1.0, 0.0],
            [
                [x0, y0, 0.0],
                [x0, y0, height],
                [x1, y0, height],
                [x1, y0, 0.0],
            ],
        );
        push_face(
            mesh,
            base_vertex + 12,
            [1.0, 0.0, 0.0],
            [
                [x1, y0, 0.0],
                [x1, y0, height],
                [x1, y1, height],
                [x1, y1, 0.0],
            ],
        );
        push_face(
            mesh,
            base_vertex + 16,
            [0.0, 1.0, 0.0],
            [
                [x1, y1, 0.0],
                [x1, y1, height],
                [x0, y1, height],
                [x0, y1, 0.0],
            ],
        );
        push_face(
            mesh,
            base_vertex + 20,
            [-1.0, 0.0, 0.0],
            [
                [x0, y1, 0.0],
                [x0, y1, height],
                [x0, y0, height],
                [x0, y0, 0.0],
            ],
        );
    }
}

fn push_face(mesh: &mut MeshBuffers, base_vertex: u32, normal: [f32; 3], corners: [[f32; 3]; 4]) {
    for (corner_idx, corner) in corners.iter().enumerate() {
        mesh.positions.extend_from_slice(corner);
        mesh.normals.extend_from_slice(&normal);
        match corner_idx {
            0 => mesh.uvs.extend_from_slice(&[0.0, 0.0]),
            1 => mesh.uvs.extend_from_slice(&[1.0, 0.0]),
            2 => mesh.uvs.extend_from_slice(&[1.0, 1.0]),
            _ => mesh.uvs.extend_from_slice(&[0.0, 1.0]),
        }
    }
    mesh.indices.extend_from_slice(&[
        base_vertex,
        base_vertex + 1,
        base_vertex + 2,
        base_vertex,
        base_vertex + 2,
        base_vertex + 3,
    ]);
}
