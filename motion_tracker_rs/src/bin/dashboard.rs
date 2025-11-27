use axum::{
    extract::{
        ws::{Message, WebSocket, WebSocketUpgrade},
        Path, Query, State,
    },
    http::StatusCode,
    response::{Html, IntoResponse, Response},
    routing::get,
    Json, Router,
};
use clap::Parser;
use flate2::read::GzDecoder;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{io::Read, net::SocketAddr, path::PathBuf, time::Duration};
use tokio::net::TcpListener;
use tokio::time::sleep;

#[derive(Parser, Debug)]
#[command(name = "dashboard")]
struct Args {
    /// Path to motion tracker output directory
    #[arg(long, default_value = "motion_tracker_sessions")]
    data_dir: PathBuf,

    /// Port to serve on
    #[arg(long, default_value = "8081")]
    port: u16,
}

#[derive(Clone)]
struct AppState {
    data_dir: PathBuf,
}

#[tokio::main]
async fn main() {
    let args = Args::parse();

    if !args.data_dir.exists() {
        eprintln!("Warning: Data directory {:?} does not exist", args.data_dir);
    }

    let state = AppState {
        data_dir: args.data_dir.clone(),
    };

    let app = Router::new()
        .route("/", get(index_handler))
        .route("/ws", get(ws_handler))
        .route("/api/drives", get(list_drives_handler))
        .route("/api/drive/:drive_id", get(drive_details_handler))
        .route("/api/drive/:drive_id/gpx", get(drive_gpx_handler))
        .with_state(state);

    let addr = SocketAddr::from(([0, 0, 0, 0], args.port));
    println!("Standalone Dashboard listening on http://{}", addr);
    println!("Watching directory: {:?}", args.data_dir);

    let listener = TcpListener::bind(addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}

async fn index_handler() -> Html<&'static str> {
    Html(include_str!("../dashboard_static.html"))
}

fn resolve_data_dir(base: &PathBuf, variant: &Option<String>) -> PathBuf {
    if let Some(v) = variant {
        if v == "rough" {
            let candidate = base.join("golden/roughness_updated");
            if candidate.exists() {
                return candidate;
            }
        }
    }
    base.clone()
}

#[derive(Deserialize)]
struct PaginationParams {
    limit: Option<u32>,
    offset: Option<u32>,
    variant: Option<String>,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
struct DriveMetadata {
    id: String,
    timestamp: String,
    datetime: String,
    duration_seconds: u64,
    accel_samples: u64,
    gps_fixes: u64,
    distance_meters: f64,
    has_gps: bool,
    file_size_mb: f64,
    is_golden: bool,
}

#[derive(Serialize)]
struct DrivesResponse {
    drives: Vec<DriveMetadata>,
    total: usize,
    offset: u32,
    limit: u32,
    #[serde(rename = "hasMore")]
    has_more: bool,
}

#[derive(Serialize)]
struct DriveDetailsResponse {
    id: String,
    timestamp: String,
    datetime: String,
    has_gps: bool,
    stats: DriveStats,
    readings: Vec<Value>,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
struct DriveStats {
    gps_samples: u64,
    accel_samples: u64,
    gyro_samples: u64,
    distance_km: f64,
    peak_memory_mb: f64,
}

fn parse_timestamp_from_filename(filename: &str) -> Option<chrono::DateTime<chrono::Utc>> {
    // Remove extensions
    let base = filename
        .replace(".json.gz", "")
        .replace(".json", "")
        .replace(".gpx", "");

    // Extract YYYYMMDD_HHMMSS pattern
    let parts: Vec<&str> = base.split('_').collect();
    if parts.len() < 2 {
        return None;
    }

    // Find date and time parts
    for i in 0..parts.len().saturating_sub(1) {
        if parts[i].len() == 8 && parts[i].chars().all(|c| c.is_numeric()) {
            if parts[i + 1].len() >= 6 && parts[i + 1][0..6].chars().all(|c| c.is_numeric()) {
                let timestamp_str = format!("{}_{}", parts[i], &parts[i + 1][0..6]);
                if let Ok(dt) =
                    chrono::NaiveDateTime::parse_from_str(&timestamp_str, "%Y%m%d_%H%M%S")
                {
                    return Some(chrono::DateTime::<chrono::Utc>::from_naive_utc_and_offset(
                        dt,
                        chrono::Utc,
                    ));
                }
            }
        }
    }
    None
}

/// Read and decompress a gzip JSON file
fn read_gzipped_json(path: &std::path::PathBuf) -> Result<Value, Box<dyn std::error::Error>> {
    let file = std::fs::File::open(path)?;
    let mut decoder = GzDecoder::new(file);
    let mut contents = String::new();
    decoder.read_to_string(&mut contents)?;
    let data = serde_json::from_str::<Value>(&contents)?;
    Ok(data)
}

fn extract_drive_stats(data: &Value) -> DriveStats {
    let mut stats = DriveStats {
        gps_samples: 0,
        accel_samples: 0,
        gyro_samples: 0,
        distance_km: 0.0,
        peak_memory_mb: 0.0,
    };

    // Try to extract from metrics (Rust format)
    if let Some(metrics) = data.get("metrics").and_then(|m| m.as_object()) {
        stats.gps_samples = metrics
            .get("gps_samples")
            .and_then(|v| v.as_u64())
            .unwrap_or(0);
        stats.accel_samples = metrics
            .get("accel_samples")
            .and_then(|v| v.as_u64())
            .unwrap_or(0);
        stats.gyro_samples = metrics
            .get("gyro_samples")
            .and_then(|v| v.as_u64())
            .unwrap_or(0);
        stats.peak_memory_mb = metrics
            .get("peak_memory_mb")
            .and_then(|v| v.as_f64())
            .unwrap_or(0.0);

        if let Some(dist) = metrics.get("ekf_distance").and_then(|v| v.as_f64()) {
            if dist > 0.0 && dist < 1_000_000.0 {
                stats.distance_km = dist / 1000.0;
            }
        }
    }

    // Fallback: check stats object (Python/Rust format)
    if stats.distance_km == 0.0 {
        if let Some(stats_obj) = data.get("stats").and_then(|s| s.as_object()) {
            if let Some(dist) = stats_obj.get("ekf_distance").and_then(|v| v.as_f64()) {
                if dist > 0.0 && dist < 1_000_000.0 {
                    stats.distance_km = dist;
                }
            }
        }
    }

    // Fallback: count readings array
    if stats.accel_samples == 0 {
        if let Some(readings) = data.get("readings").and_then(|r| r.as_array()) {
            stats.accel_samples = readings.len() as u64;
        }
    }

    // Count GPS fixes in readings
    if let Some(readings) = data.get("readings").and_then(|r| r.as_array()) {
        let gps_fix_count = readings
            .iter()
            .filter(|r| r.get("gps").and_then(|g| g.get("latitude")).is_some())
            .count() as u64;
        if stats.gps_samples == 0 {
            stats.gps_samples = gps_fix_count;
        }
    }

    stats
}

fn has_gps_data(data: &Value) -> bool {
    // Quick check in metrics
    if let Some(gps_samples) = data
        .get("metrics")
        .and_then(|m| m.get("gps_samples"))
        .and_then(|v| v.as_u64())
    {
        if gps_samples > 0 {
            return true;
        }
    }

    // Check readings array (first 1000 entries)
    if let Some(readings) = data.get("readings").and_then(|r| r.as_array()) {
        for reading in readings.iter().take(1000) {
            if reading.get("gps").and_then(|g| g.get("latitude")).is_some() {
                return true;
            }
        }
    }

    false
}

async fn list_drives_handler(
    State(state): State<AppState>,
    Query(params): Query<PaginationParams>,
) -> Result<Json<DrivesResponse>, (StatusCode, String)> {
    let limit = params.limit.unwrap_or(20).min(100) as usize;
    let offset = params.offset.unwrap_or(0) as usize;
    let base_dir = resolve_data_dir(&state.data_dir, &params.variant);

    // Scan directory for drive files (compressed gzip JSON)
    // Collect (path, is_golden)
    let mut filepaths: Vec<(std::path::PathBuf, bool)> = Vec::new();

    match std::fs::read_dir(&base_dir) {
        Ok(entries) => {
            for entry in entries {
                if let Ok(entry) = entry {
                    let path = entry.path();
                    if let Some(name) = path.file_name().and_then(|n| n.to_str()) {
                        // List finalized drive files (drive_*.json.gz) and comparison logs (comparison_*.json.gz)
                        if (name.starts_with("drive_") || name.starts_with("comparison_"))
                            && name.ends_with(".json.gz")
                        {
                            filepaths.push((path, false));
                        }
                    }
                }
            }
        }
        Err(e) => return Err((StatusCode::INTERNAL_SERVER_ERROR, e.to_string())),
    }

    // Also include curated golden drives if present (comparison_*.json.gz in golden/)
    let golden_dir = base_dir.join("golden");
    if let Ok(entries) = std::fs::read_dir(&golden_dir) {
        for entry in entries.flatten() {
            let path = entry.path();
            if let Some(name) = path.file_name().and_then(|n| n.to_str()) {
                if name.starts_with("comparison_") && name.ends_with(".json.gz") {
                    filepaths.push((path, true));
                }
            }
        }
    }

    // Sort by modification time descending (newest first)
    filepaths.sort_by(|a, b| {
        let mtime_a = std::fs::metadata(&a.0)
            .ok()
            .and_then(|m| m.modified().ok())
            .unwrap_or(std::time::SystemTime::UNIX_EPOCH);
        let mtime_b = std::fs::metadata(&b.0)
            .ok()
            .and_then(|m| m.modified().ok())
            .unwrap_or(std::time::SystemTime::UNIX_EPOCH);
        mtime_b.cmp(&mtime_a)
    });

    let total = filepaths.len();
    let paginated: Vec<_> = filepaths.into_iter().skip(offset).take(limit).collect();

    let mut drives = Vec::new();

    for (filepath, is_golden) in paginated {
        if let Ok(data) = read_gzipped_json(&filepath) {
            if let Some(filename) = filepath.file_name().and_then(|n| n.to_str()) {
                let drive_id = filename.replace(".json.gz", "");

                let timestamp = parse_timestamp_from_filename(filename)
                    .map(|dt| dt.to_rfc3339())
                    .unwrap_or_default();
                let datetime = parse_timestamp_from_filename(filename)
                    .map(|dt| dt.format("%Y-%m-%d %H:%M:%S").to_string())
                    .unwrap_or_default();

                let stats = extract_drive_stats(&data);
                let has_gps = has_gps_data(&data);

                let file_size = std::fs::metadata(&filepath)
                    .ok()
                    .map(|m| m.len() as f64 / (1024.0 * 1024.0))
                    .unwrap_or(0.0);

                // Calculate duration from first and last reading timestamps
                let duration_seconds =
                    if let Some(readings) = data.get("readings").and_then(|r| r.as_array()) {
                        if readings.len() > 1 {
                            let first_ts = readings[0]
                                .get("timestamp")
                                .and_then(|v| v.as_f64())
                                .unwrap_or(0.0);
                            let last_ts = readings[readings.len() - 1]
                                .get("timestamp")
                                .and_then(|v| v.as_f64())
                                .unwrap_or(0.0);
                            (last_ts - first_ts).max(0.0) as u64
                        } else {
                            0
                        }
                    } else {
                        0
                    };

                drives.push(DriveMetadata {
                    id: drive_id,
                    timestamp,
                    datetime,
                    duration_seconds,
                    accel_samples: stats.accel_samples,
                    gps_fixes: stats.gps_samples,
                    distance_meters: stats.distance_km * 1000.0,
                    has_gps,
                    file_size_mb: file_size,
                    is_golden,
                });
            }
        }
    }

    Ok(Json(DrivesResponse {
        drives,
        total,
        offset: offset as u32,
        limit: limit as u32,
        has_more: (offset + limit) < total,
    }))
}

async fn drive_details_handler(
    State(state): State<AppState>,
    Path(drive_id): Path<String>,
    Query(params): Query<PaginationParams>,
) -> Result<Json<DriveDetailsResponse>, (StatusCode, String)> {
    // Find the file (gzipped format). Try root, then golden/.
    let base_dir = resolve_data_dir(&state.data_dir, &params.variant);
    let mut json_path = base_dir.join(format!("{}.json.gz", drive_id));
    if !json_path.exists() {
        let golden_candidate = base_dir
            .join("golden")
            .join(format!("{}.json.gz", drive_id));
        if golden_candidate.exists() {
            json_path = golden_candidate;
        } else {
            return Err((StatusCode::NOT_FOUND, "Drive not found".to_string()));
        }
    }

    let data =
        read_gzipped_json(&json_path).map_err(|e| (StatusCode::BAD_REQUEST, e.to_string()))?;

    let timestamp = parse_timestamp_from_filename(&drive_id)
        .map(|dt| dt.to_rfc3339())
        .unwrap_or_default();
    let datetime = parse_timestamp_from_filename(&drive_id)
        .map(|dt| dt.format("%Y-%m-%d %H:%M:%S").to_string())
        .unwrap_or_default();

    let stats = extract_drive_stats(&data);
    let has_gps = has_gps_data(&data);

    // Extract readings array
    let readings = data
        .get("readings")
        .and_then(|r| r.as_array())
        .map(|arr| arr.clone())
        .unwrap_or_default();

    Ok(Json(DriveDetailsResponse {
        id: drive_id,
        timestamp,
        datetime,
        has_gps,
        stats,
        readings,
    }))
}

async fn drive_gpx_handler(
    State(state): State<AppState>,
    Path(drive_id): Path<String>,
    Query(params): Query<PaginationParams>,
) -> Result<Response, (StatusCode, String)> {
    let base_dir = resolve_data_dir(&state.data_dir, &params.variant);
    let mut json_path = base_dir.join(format!("{}.json.gz", drive_id));
    if !json_path.exists() {
        let golden_candidate = base_dir
            .join("golden")
            .join(format!("{}.json.gz", drive_id));
        if golden_candidate.exists() {
            json_path = golden_candidate;
        } else {
            return Err((StatusCode::NOT_FOUND, "Drive not found".to_string()));
        }
    }

    let data =
        read_gzipped_json(&json_path).map_err(|e| (StatusCode::BAD_REQUEST, e.to_string()))?;

    let gpx = generate_gpx_from_json(&data)?;

    Ok((
        [(axum::http::header::CONTENT_TYPE, "application/gpx+xml")],
        gpx,
    )
        .into_response())
}

fn generate_gpx_from_json(data: &Value) -> Result<String, (StatusCode, String)> {
    let mut gps_points = Vec::new();

    // Extract GPS points from readings array
    if let Some(readings) = data.get("readings").and_then(|r| r.as_array()) {
        for reading in readings {
            if let Some(gps) = reading.get("gps").and_then(|g| g.as_object()) {
                if let (Some(lat), Some(lon)) = (
                    gps.get("latitude").and_then(|v| v.as_f64()),
                    gps.get("longitude").and_then(|v| v.as_f64()),
                ) {
                    let ele = gps.get("altitude").and_then(|v| v.as_f64()).unwrap_or(0.0);
                    let ts = reading
                        .get("timestamp")
                        .and_then(|v| v.as_f64())
                        .map(|t| t.to_string())
                        .unwrap_or_default();
                    gps_points.push((lat, lon, ele, ts));
                }
            }
        }
    }

    if gps_points.is_empty() {
        return Err((
            StatusCode::BAD_REQUEST,
            "No GPS data found in drive".to_string(),
        ));
    }

    let now = chrono::Utc::now().to_rfc3339();
    let mut gpx_lines = vec![
        r#"<?xml version="1.0" encoding="UTF-8"?>"#.to_string(),
        r#"<gpx version="1.1" creator="Motion Tracker Rust Dashboard">"#.to_string(),
        "  <metadata>".to_string(),
        format!("    <time>{}</time>", now),
        "    <desc>GPS trajectory</desc>".to_string(),
        "  </metadata>".to_string(),
        "  <trk>".to_string(),
        "    <name>GPS Track</name>".to_string(),
        "    <trkseg>".to_string(),
    ];

    for (lat, lon, ele, _ts) in gps_points {
        gpx_lines.push(format!(r#"      <trkpt lat="{}" lon="{}">"#, lat, lon));
        if ele != 0.0 {
            gpx_lines.push(format!("        <ele>{}</ele>", ele));
        }
        gpx_lines.push("      </trkpt>".to_string());
    }

    gpx_lines.extend(vec![
        "    </trkseg>".to_string(),
        "  </trk>".to_string(),
        "</gpx>".to_string(),
    ]);

    Ok(gpx_lines.join("\n"))
}

async fn ws_handler(ws: WebSocketUpgrade, State(state): State<AppState>) -> impl IntoResponse {
    ws.on_upgrade(|socket| handle_socket(socket, state))
}

#[derive(Deserialize, Serialize, Default)]
struct LiveStatus {
    timestamp: f64,
    accel_samples: u64,
    gyro_samples: u64,
    gps_fixes: u64,
    gps_speed: Option<f64>,
    gps_bearing: Option<f64>,
    gps_lat: Option<f64>,
    gps_lon: Option<f64>,
    gravity_magnitude: Option<f64>,
    uptime_seconds: u64,
    #[serde(default)]
    accel_x: f64,
    #[serde(default)]
    accel_y: f64,
    #[serde(default)]
    accel_z: f64,
}

async fn handle_socket(mut socket: WebSocket, state: AppState) {
    let status_file = state.data_dir.join("live_status.json");
    let mut last_mtime = std::time::SystemTime::UNIX_EPOCH;

    loop {
        if let Ok(metadata) = std::fs::metadata(&status_file) {
            if let Ok(mtime) = metadata.modified() {
                if mtime > last_mtime {
                    last_mtime = mtime;

                    if let Ok(content) = tokio::fs::read_to_string(&status_file).await {
                        if let Ok(status) = serde_json::from_str::<LiveStatus>(&content) {
                            let json = serde_json::to_string(&status).unwrap();
                            if socket.send(Message::Text(json)).await.is_err() {
                                break;
                            }
                        }
                    }
                }
            }
        }

        sleep(Duration::from_millis(500)).await;
    }
}
