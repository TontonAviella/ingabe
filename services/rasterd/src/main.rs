use std::collections::hash_map::DefaultHasher;
use std::ffi::CString;
use std::hash::{Hash, Hasher};
use std::net::SocketAddr;
use std::num::NonZeroUsize;
use std::os::raw::c_char;
use std::path::Path;
use std::ptr;
use std::slice;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Instant;

use anyhow::{anyhow, bail, Context, Result};
use axum::extract::{Path as AxumPath, Query, State};
use axum::http::{header, HeaderMap, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use gdal::raster::ResampleAlg;
use gdal::Dataset;
use gdal_sys::{
    GDALClose, GDALDatasetH, GDALWarp, GDALWarpAppOptionsFree, GDALWarpAppOptionsNew, VSIFree,
    VSIGetMemFileBuffer, VSIUnlink,
};
use image::codecs::png::PngEncoder;
use image::{ColorType, ImageEncoder, Rgba, RgbaImage};
use lru::LruCache;
use serde::{Deserialize, Serialize};
use tokio::sync::Semaphore;
use tower_http::trace::TraceLayer;
use tracing::{debug, info, warn};

const TILE_SIZE: usize = 256;
const WEB_MERCATOR_HALF_WORLD: f64 = 20037508.342789244;
static VSIMEM_TILE_COUNTER: AtomicU64 = AtomicU64::new(1);

struct GdalArgv {
    _strings: Vec<CString>,
    ptrs: Vec<*mut c_char>,
}

impl GdalArgv {
    fn new(args: &[String]) -> Result<Self> {
        let mut strings = Vec::with_capacity(args.len());
        let mut ptrs = Vec::with_capacity(args.len() + 1);
        for arg in args {
            let c = CString::new(arg.as_str())?;
            ptrs.push(c.as_ptr() as *mut c_char);
            strings.push(c);
        }
        ptrs.push(ptr::null_mut());
        Ok(Self {
            _strings: strings,
            ptrs,
        })
    }

    fn as_mut_ptr(&mut self) -> *mut *mut c_char {
        self.ptrs.as_mut_ptr()
    }
}

#[derive(Clone)]
struct AppState {
    cache: Arc<Mutex<LruCache<String, Arc<Vec<u8>>>>>,
    dataset_cache: Arc<Mutex<LruCache<String, Arc<Mutex<Dataset>>>>>,
    render_permits: Arc<Semaphore>,
}

#[derive(Debug, Deserialize)]
struct TileQuery {
    url: String,
    layer_id: Option<String>,
    bands: Option<String>,
}

#[derive(Debug, Serialize)]
struct CacheStats {
    len: usize,
    cap: usize,
    dataset_len: usize,
    dataset_cap: usize,
}

#[derive(Debug)]
enum TileError {
    Outside,
    Unsupported(String),
    Render(anyhow::Error),
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            std::env::var("RUST_LOG")
                .unwrap_or_else(|_| "mundi_rasterd=info,tower_http=warn".to_string()),
        )
        .init();

    #[cfg(feature = "forge3d-cog")]
    info!("experimental forge3d-cog feature marker enabled; raster renderer remains GDAL-backed");

    let cache_cap = std::env::var("RASTERD_TILE_CACHE_ENTRIES")
        .ok()
        .and_then(|s| s.parse::<usize>().ok())
        .filter(|v| *v > 0)
        .unwrap_or(4096);
    let render_concurrency = std::env::var("RASTERD_RENDER_CONCURRENCY")
        .ok()
        .and_then(|s| s.parse::<usize>().ok())
        .filter(|v| *v > 0)
        .unwrap_or(1);
    let dataset_cache_cap = std::env::var("RASTERD_DATASET_CACHE_ENTRIES")
        .ok()
        .and_then(|s| s.parse::<usize>().ok())
        .filter(|v| *v > 0)
        .unwrap_or(16);
    let state = AppState {
        cache: Arc::new(Mutex::new(LruCache::new(
            NonZeroUsize::new(cache_cap).expect("cache cap is non-zero"),
        ))),
        dataset_cache: Arc::new(Mutex::new(LruCache::new(
            NonZeroUsize::new(dataset_cache_cap).expect("dataset cache cap is non-zero"),
        ))),
        render_permits: Arc::new(Semaphore::new(render_concurrency)),
    };

    let app = Router::new()
        .route("/healthz", get(healthz))
        .route("/debug/cache", get(debug_cache))
        .route("/tiles/:z/:x/:y.png", get(tile_png))
        .layer(TraceLayer::new_for_http())
        .with_state(state);

    let port = std::env::var("RASTERD_PORT")
        .ok()
        .and_then(|s| s.parse::<u16>().ok())
        .unwrap_or(8877);
    let addr = SocketAddr::from(([0, 0, 0, 0], port));
    info!("mundi-rasterd listening on {addr} render_concurrency={render_concurrency}");

    let listener = tokio::net::TcpListener::bind(addr).await?;
    axum::serve(listener, app).await?;
    Ok(())
}

async fn healthz() -> Json<serde_json::Value> {
    Json(serde_json::json!({
        "status": "ok",
        "engine": "mundi-rasterd",
        "renderer": "gdal-webmercator-cog-or-raw-warp",
        "forge3d_cog_feature": cfg!(feature = "forge3d-cog"),
        "forge3d_runtime": "python-impact-adapter",
    }))
}

async fn debug_cache(State(state): State<AppState>) -> Json<CacheStats> {
    let cache = state.cache.lock().expect("cache lock");
    let dataset_cache = state.dataset_cache.lock().expect("dataset cache lock");
    Json(CacheStats {
        len: cache.len(),
        cap: cache.cap().get(),
        dataset_len: dataset_cache.len(),
        dataset_cap: dataset_cache.cap().get(),
    })
}

async fn tile_png(
    State(state): State<AppState>,
    AxumPath((z, x, y_raw)): AxumPath<(u32, u32, String)>,
    Query(query): Query<TileQuery>,
) -> Response {
    let y = match parse_tile_y(&y_raw) {
        Ok(value) => value,
        Err(e) => return error_response(StatusCode::BAD_REQUEST, e.to_string()),
    };
    let bands = match parse_bands(query.bands.as_deref()) {
        Ok(b) => b,
        Err(e) => return error_response(StatusCode::BAD_REQUEST, e.to_string()),
    };

    let cache_key = cache_key(query.layer_id.as_deref(), &query.url, z, x, y, &bands);
    if let Some(bytes) = state
        .cache
        .lock()
        .expect("cache lock")
        .get(&cache_key)
        .cloned()
    {
        debug!("tile cache hit z={z} x={x} y={y}");
        return png_response(bytes.as_ref().clone());
    }

    let render_permit = match state.render_permits.clone().acquire_owned().await {
        Ok(permit) => permit,
        Err(e) => {
            return error_response(
                StatusCode::SERVICE_UNAVAILABLE,
                format!("render limiter closed: {e}"),
            )
        }
    };
    let started = Instant::now();
    let url = query.url.clone();
    let dataset_cache = state.dataset_cache.clone();
    let render = tokio::task::spawn_blocking(move || {
        render_webmercator_tile(&dataset_cache, &url, z, x, y, &bands)
    })
    .await
    .unwrap_or_else(|e| Err(TileError::Render(anyhow!("tile worker join failed: {e}"))));
    drop(render_permit);
    let elapsed_ms = started.elapsed().as_millis();

    match render {
        Ok(bytes) => {
            if elapsed_ms > 500 {
                info!(
                    "tile render z={z} x={x} y={y} bytes={} elapsed_ms={elapsed_ms}",
                    bytes.len()
                );
            } else {
                debug!(
                    "tile render z={z} x={x} y={y} bytes={} elapsed_ms={elapsed_ms}",
                    bytes.len()
                );
            }
            let bytes = Arc::new(bytes);
            state
                .cache
                .lock()
                .expect("cache lock")
                .put(cache_key, bytes.clone());
            png_response(bytes.as_ref().clone())
        }
        Err(TileError::Outside) => {
            debug!("tile outside z={z} x={x} y={y} elapsed_ms={elapsed_ms}");
            StatusCode::NO_CONTENT.into_response()
        }
        Err(TileError::Unsupported(msg)) => error_response(StatusCode::NOT_IMPLEMENTED, msg),
        Err(TileError::Render(err)) => {
            warn!("tile render failed z={z} x={x} y={y} elapsed_ms={elapsed_ms}: {err:#}");
            error_response(
                StatusCode::BAD_GATEWAY,
                "raster tile render failed".to_string(),
            )
        }
    }
}

fn png_response(bytes: Vec<u8>) -> Response {
    let mut headers = HeaderMap::new();
    headers.insert(header::CONTENT_TYPE, HeaderValue::from_static("image/png"));
    headers.insert(
        header::CACHE_CONTROL,
        HeaderValue::from_static("public, max-age=3600"),
    );
    (headers, bytes).into_response()
}

fn error_response(status: StatusCode, message: String) -> Response {
    (status, Json(serde_json::json!({"error": message}))).into_response()
}

fn parse_tile_y(raw: &str) -> Result<u32> {
    raw.strip_suffix(".png")
        .unwrap_or(raw)
        .parse::<u32>()
        .with_context(|| format!("invalid tile y: {raw}"))
}

fn parse_bands(raw: Option<&str>) -> Result<Vec<usize>> {
    let raw = raw.unwrap_or("1,2,3");
    let mut out = Vec::new();
    for part in raw.split(',') {
        let band = part
            .trim()
            .parse::<usize>()
            .with_context(|| format!("invalid band index: {part}"))?;
        if band == 0 {
            bail!("band indexes are 1-based");
        }
        out.push(band);
    }
    if out.is_empty() || out.len() > 4 {
        bail!("bands must contain 1 to 4 indexes");
    }
    Ok(out)
}

fn default_raw_bands(band_count: usize) -> Option<Vec<usize>> {
    if band_count == 0 {
        None
    } else if band_count == 1 {
        Some(vec![1])
    } else if band_count == 2 {
        Some(vec![1, 2])
    } else {
        Some(vec![1, 2, 3])
    }
}

fn cache_key(layer_id: Option<&str>, url: &str, z: u32, x: u32, y: u32, bands: &[usize]) -> String {
    let stable_id = layer_id.map(str::to_string).unwrap_or_else(|| {
        let mut h = DefaultHasher::new();
        url.hash(&mut h);
        format!("url:{:x}", h.finish())
    });
    format!("{stable_id}:{z}:{x}:{y}:{bands:?}:png")
}

fn render_webmercator_tile(
    dataset_cache: &Arc<Mutex<LruCache<String, Arc<Mutex<Dataset>>>>>,
    url: &str,
    z: u32,
    x: u32,
    y: u32,
    bands: &[usize],
) -> std::result::Result<Vec<u8>, TileError> {
    let dataset_handle = open_dataset(dataset_cache, url)?;
    let dataset = dataset_handle
        .lock()
        .map_err(|_| TileError::Render(anyhow!("dataset cache lock poisoned")))?;

    let auth_code = dataset
        .spatial_ref()
        .ok()
        .and_then(|srs| srs.auth_code().ok());
    if auth_code != Some(3857) {
        return render_raw_webmercator_tile(&dataset, z, x, y, bands).map_err(TileError::Render);
    }

    let gt = dataset
        .geo_transform()
        .map_err(|e| TileError::Render(e.into()))?;
    if gt[2].abs() > 1e-9 || gt[4].abs() > 1e-9 {
        return Err(TileError::Unsupported(
            "rotated/skewed rasters are not supported by rasterd V1".to_string(),
        ));
    }

    let (minx, miny, maxx, maxy) = webmercator_bounds(z, x, y)?;
    let (px_left, py_top) = world_to_pixel(&gt, minx, maxy);
    let (px_right, py_bottom) = world_to_pixel(&gt, maxx, miny);

    let left = px_left.floor() as isize;
    let top = py_top.floor() as isize;
    let right = px_right.ceil() as isize;
    let bottom = py_bottom.ceil() as isize;

    let (raster_w, raster_h) = dataset.raster_size();
    let full_w = right - left;
    let full_h = bottom - top;
    if full_w <= 0 || full_h <= 0 {
        return Err(TileError::Outside);
    }

    let clip_left = left.max(0);
    let clip_top = top.max(0);
    let clip_right = right.min(raster_w as isize);
    let clip_bottom = bottom.min(raster_h as isize);
    if clip_left >= clip_right || clip_top >= clip_bottom {
        return Err(TileError::Outside);
    }

    let out_x0 = (((clip_left - left) as f64 / full_w as f64) * TILE_SIZE as f64)
        .floor()
        .clamp(0.0, TILE_SIZE as f64) as usize;
    let out_y0 = (((clip_top - top) as f64 / full_h as f64) * TILE_SIZE as f64)
        .floor()
        .clamp(0.0, TILE_SIZE as f64) as usize;
    let out_x1 = (((clip_right - left) as f64 / full_w as f64) * TILE_SIZE as f64)
        .ceil()
        .clamp(0.0, TILE_SIZE as f64) as usize;
    let out_y1 = (((clip_bottom - top) as f64 / full_h as f64) * TILE_SIZE as f64)
        .ceil()
        .clamp(0.0, TILE_SIZE as f64) as usize;
    let out_w = out_x1.saturating_sub(out_x0);
    let out_h = out_y1.saturating_sub(out_y0);
    if out_w == 0 || out_h == 0 {
        return Err(TileError::Outside);
    }

    let win_w = usize::try_from(clip_right - clip_left).unwrap_or(0);
    let win_h = usize::try_from(clip_bottom - clip_top).unwrap_or(0);
    let mut band_data: Vec<Vec<u8>> = Vec::with_capacity(bands.len());
    for &band_index in bands {
        let band = dataset
            .rasterband(band_index)
            .map_err(|e| TileError::Render(e.into()))?;
        let buffer = band
            .read_as::<u8>(
                (clip_left, clip_top),
                (win_w, win_h),
                (out_w, out_h),
                Some(ResampleAlg::Bilinear),
            )
            .map_err(|e| TileError::Render(e.into()))?;
        band_data.push(buffer.data().to_vec());
    }

    encode_rgba_png(&band_data, out_w, out_h, out_x0, out_y0).map_err(TileError::Render)
}

fn open_dataset(
    dataset_cache: &Arc<Mutex<LruCache<String, Arc<Mutex<Dataset>>>>>,
    url: &str,
) -> std::result::Result<Arc<Mutex<Dataset>>, TileError> {
    let open_path = gdal_open_path(url);
    {
        let mut cache = dataset_cache
            .lock()
            .map_err(|_| TileError::Render(anyhow!("dataset cache lock poisoned")))?;
        if let Some(dataset) = cache.get(&open_path).cloned() {
            return Ok(dataset);
        }
    }

    let dataset = Dataset::open(Path::new(&open_path)).map_err(|e| TileError::Render(e.into()))?;
    let dataset = Arc::new(Mutex::new(dataset));
    let mut cache = dataset_cache
        .lock()
        .map_err(|_| TileError::Render(anyhow!("dataset cache lock poisoned")))?;
    if let Some(existing) = cache.get(&open_path).cloned() {
        return Ok(existing);
    }
    cache.put(open_path, dataset.clone());
    Ok(dataset)
}

fn render_raw_webmercator_tile(
    dataset: &Dataset,
    z: u32,
    x: u32,
    y: u32,
    bands: &[usize],
) -> Result<Vec<u8>> {
    let (raster_w, raster_h) = dataset.raster_size();
    let band_count = dataset.raster_count();
    if raster_w == 0 || raster_h == 0 {
        return Err(anyhow!("raw raster has no readable bands or pixels"));
    }
    let Some(default_bands) = default_raw_bands(band_count) else {
        return Err(anyhow!("raw raster has no readable bands or pixels"));
    };
    if bands != default_bands.as_slice() {
        return Err(anyhow!(
            "raw raster band selection requires GDAL 3.7+; requested {bands:?}, default for this raster is {default_bands:?}"
        ));
    }

    let (minx, miny, maxx, maxy) = webmercator_bounds(z, x, y)
        .map_err(|e| anyhow!("invalid WebMercator tile bounds: {e:?}"))?;
    let seq = VSIMEM_TILE_COUNTER.fetch_add(1, Ordering::Relaxed);
    let tile_path = format!(
        "/vsimem/mundi-rasterd-{}-{seq}-{z}-{x}-{y}.png",
        std::process::id()
    );
    let c_tile_path = CString::new(tile_path.as_str())?;

    let mut args = vec![
        "-q".to_string(),
        "-overwrite".to_string(),
        "-t_srs".to_string(),
        "EPSG:3857".to_string(),
        "-te_srs".to_string(),
        "EPSG:3857".to_string(),
        "-te".to_string(),
        minx.to_string(),
        miny.to_string(),
        maxx.to_string(),
        maxy.to_string(),
        "-ts".to_string(),
        TILE_SIZE.to_string(),
        TILE_SIZE.to_string(),
        "-r".to_string(),
        "bilinear".to_string(),
        "-nosrcalpha".to_string(),
    ];
    args.push("-of".to_string());
    args.push("PNG".to_string());

    let mut argv = GdalArgv::new(&args)?;
    unsafe {
        let _ = VSIUnlink(c_tile_path.as_ptr());
        let options = GDALWarpAppOptionsNew(argv.as_mut_ptr(), ptr::null_mut());
        if options.is_null() {
            return Err(anyhow!("GDALWarpAppOptionsNew failed"));
        }

        let mut src_datasets: [GDALDatasetH; 1] = [dataset.c_dataset()];
        let mut usage_error = 0;
        let output = GDALWarp(
            c_tile_path.as_ptr(),
            ptr::null_mut(),
            1,
            src_datasets.as_mut_ptr(),
            options,
            &mut usage_error,
        );
        GDALWarpAppOptionsFree(options);
        if output.is_null() || usage_error != 0 {
            let _ = VSIUnlink(c_tile_path.as_ptr());
            return Err(anyhow!(
                "raw GDAL tile warp failed usage_error={usage_error}"
            ));
        }
        GDALClose(output);

        let mut len = 0;
        let data = VSIGetMemFileBuffer(c_tile_path.as_ptr(), &mut len, 1);
        if data.is_null() || len == 0 {
            let _ = VSIUnlink(c_tile_path.as_ptr());
            return Err(anyhow!("raw GDAL tile warp produced no PNG bytes"));
        }
        let bytes = slice::from_raw_parts(data, len as usize).to_vec();
        VSIFree(data.cast());
        Ok(bytes)
    }
}

fn gdal_open_path(url: &str) -> String {
    if url.starts_with("http://") || url.starts_with("https://") {
        format!("/vsicurl/{url}")
    } else {
        url.to_string()
    }
}

fn webmercator_bounds(
    z: u32,
    x: u32,
    y: u32,
) -> std::result::Result<(f64, f64, f64, f64), TileError> {
    if z > 30 || x >= (1u32 << z) || y >= (1u32 << z) {
        return Err(TileError::Render(anyhow!("invalid z/x/y")));
    }
    let n = 2_f64.powi(z as i32);
    let span = WEB_MERCATOR_HALF_WORLD * 2.0;
    let minx = x as f64 / n * span - WEB_MERCATOR_HALF_WORLD;
    let maxx = (x as f64 + 1.0) / n * span - WEB_MERCATOR_HALF_WORLD;
    let maxy = WEB_MERCATOR_HALF_WORLD - y as f64 / n * span;
    let miny = WEB_MERCATOR_HALF_WORLD - (y as f64 + 1.0) / n * span;
    Ok((minx, miny, maxx, maxy))
}

fn world_to_pixel(gt: &[f64; 6], x: f64, y: f64) -> (f64, f64) {
    ((x - gt[0]) / gt[1], (y - gt[3]) / gt[5])
}

fn encode_rgba_png(
    bands: &[Vec<u8>],
    sample_w: usize,
    sample_h: usize,
    offset_x: usize,
    offset_y: usize,
) -> Result<Vec<u8>> {
    let pixels = sample_w * sample_h;
    for band in bands {
        if band.len() < pixels {
            bail!("band buffer shorter than expected");
        }
    }
    let mut image = RgbaImage::new(TILE_SIZE as u32, TILE_SIZE as u32);
    for i in 0..pixels {
        let rgba = match bands.len() {
            1 => {
                let v = bands[0][i];
                Rgba([v, v, v, 255])
            }
            2 => {
                let v = bands[0][i];
                Rgba([v, v, v, bands[1][i]])
            }
            3 => Rgba([bands[0][i], bands[1][i], bands[2][i], 255]),
            _ => Rgba([bands[0][i], bands[1][i], bands[2][i], bands[3][i]]),
        };
        let px = (offset_x + (i % sample_w)) as u32;
        let py = (offset_y + (i / sample_w)) as u32;
        image.put_pixel(px, py, rgba);
    }

    let mut out = Vec::new();
    let encoder = PngEncoder::new(&mut out);
    encoder.write_image(
        image.as_raw(),
        TILE_SIZE as u32,
        TILE_SIZE as u32,
        ColorType::Rgba8.into(),
    )?;
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_raw_bands_match_png_safe_upload_defaults() {
        assert_eq!(default_raw_bands(0), None);
        assert_eq!(default_raw_bands(1), Some(vec![1]));
        assert_eq!(default_raw_bands(2), Some(vec![1, 2]));
        assert_eq!(default_raw_bands(3), Some(vec![1, 2, 3]));
        assert_eq!(default_raw_bands(4), Some(vec![1, 2, 3]));
    }
}
