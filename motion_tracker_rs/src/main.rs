use anyhow::Result;
use chrono::Utc;
use clap::Parser;
use flate2::write::GzEncoder;
use flate2::Compression;
use serde::{Deserialize, Serialize};
use std::collections::VecDeque;
use std::fs::{File, OpenOptions};
use std::io::{BufWriter, Write};
use std::panic;
use std::process::Stdio;
use std::sync::Arc;
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::Command;
use tokio::sync::mpsc;
use tokio::sync::RwLock;
use tokio::time::{sleep, Duration};

mod dashboard;
mod health_monitor;
mod live_status;
mod physics;
mod rerun_logger;
mod restart_manager;

use motion_tracker_rs::filters;
use motion_tracker_rs::incident;
use motion_tracker_rs::sensor_fusion;
use motion_tracker_rs::types;

use sensor_fusion::{FusionConfig, FusionEvent, SensorFusion};
use rerun_logger::RerunLogger;
use types::{AccelData, GpsData, GyroData};

/// Log to file for debugging (bypasses stdout which may be corrupted)
fn debug_log(msg: &str) {
    if let Ok(mut file) = OpenOptions::new()
        .create(true)
        .append(true)
        .open("motion_tracker_debug.log")
    {
        let _ = writeln!(file, "[{}] {}", Utc::now().format("%H:%M:%S%.3f"), msg);
    }
}

/// Get current memory usage in MB from /proc/self/status
fn get_memory_mb() -> f64 {
    if let Ok(content) = std::fs::read_to_string("/proc/self/status") {
        for line in content.lines() {
            if line.starts_with("VmRSS:") {
                if let Some(value) = line.split_whitespace().nth(1) {
                    if let Ok(kb) = value.parse::<f64>() {
                        return kb / 1024.0;
                    }
                }
            }
        }
    }
    0.0
}

#[derive(Parser, Debug)]
#[command(name = "motion_tracker")]
#[command(about = "Rust motion tracker - EKF vs Complementary filter comparison", long_about = None)]
struct Args {
    /// Duration in seconds (0 = continuous)
    #[arg(value_name = "SECONDS", default_value = "0")]
    duration: u64,

    /// Enable gyroscope processing (default: true)
    #[arg(long, default_value = "true")]
    enable_gyro: bool,

    /// Filter type (ekf, complementary, both)
    #[arg(long, default_value = "both")]
    filter: String,

    /// Output directory
    #[arg(
        long,
        default_value = "/data/data/com.termux/files/home/gojo/motion_tracker_sessions"
    )]
    output_dir: String,

    /// Dashboard port (default: 8080)
    #[arg(long, default_value = "8080")]
    dashboard_port: u16,

    /// Enable magnetometer fusion (still collected if off)
    #[arg(long, default_value_t = false)]
    enable_mag: bool,

    /// Enable barometer-based vertical constraint (still collected if off)
    #[arg(long, default_value_t = false)]
    enable_baro: bool,
}

#[derive(Serialize, Deserialize, Clone)]
struct SensorReading {
    timestamp: f64,
    accel: Option<AccelData>,
    gyro: Option<GyroData>,
    mag: Option<types::MagData>,
    baro: Option<types::BaroData>,
    gps: Option<GpsData>,
    roughness: Option<f64>,
    specific_power_w_per_kg: f64,
    power_coefficient: f64,
    experimental_13d: Option<filters::ekf_13d::Ekf13dState>,
    experimental_15d: Option<filters::ekf_15d::Ekf15dState>,
    fgo: Option<filters::fgo::FgoState>,
}

#[derive(Serialize, Deserialize, Clone)]
struct TrajectoryPoint {
    timestamp: f64,
    ekf_x: f64,
    ekf_y: f64,
    ekf_velocity: f64,
    ekf_heading_deg: f64,
    comp_velocity: f64,
}

#[derive(Serialize, Deserialize, Clone)]
struct CovarianceSnapshot {
    timestamp: f64,
    trace: f64,
    p00: f64,
    p11: f64,
    p22: f64,
    p33: f64,
    p44: f64,
    p55: f64,
    p66: f64,
    p77: f64,
}

#[derive(Serialize, Deserialize)]
struct ComparisonOutput {
    readings: Vec<SensorReading>,
    incidents: Vec<incident::Incident>,
    trajectories: Vec<TrajectoryPoint>,
    stats: Stats,
    metrics: Metrics,
    system_health: String,
    track_path: Vec<[f64; 2]>,
}

#[derive(Serialize, Deserialize)]
struct Stats {
    total_samples: usize,
    total_incidents: usize,
    ekf_velocity: f64,
    ekf_distance: f64,
    gps_fixes: u64,
}

#[derive(Serialize, Deserialize)]
struct Metrics {
    test_duration_seconds: u64,
    accel_samples: u64,
    gyro_samples: u64,
    gps_samples: u64,
    gravity_magnitude: f64,
    gravity_x: f64,
    gravity_y: f64,
    gravity_z: f64,
    gyro_bias_x: f64,
    gyro_bias_y: f64,
    gyro_bias_z: f64,
    calibration_complete: bool,
    // Dynamic calibration tracking
    gravity_refinements: u64,
    gravity_drift_magnitude: f64,
    gravity_final_x: f64,
    gravity_final_y: f64,
    gravity_final_z: f64,
    peak_memory_mb: f64,
    current_memory_mb: f64,
    covariance_snapshots: Vec<CovarianceSnapshot>,
}

/// Shared sensor state using RwLock for minimal contention
#[derive(Clone)]
struct SensorState {
    pub accel_buffer: Arc<RwLock<VecDeque<AccelData>>>,
    pub gyro_buffer: Arc<RwLock<VecDeque<GyroData>>>,
    pub mag_buffer: Arc<RwLock<VecDeque<types::MagData>>>,
    pub baro_buffer: Arc<RwLock<VecDeque<types::BaroData>>>,
    pub latest_accel: Arc<RwLock<Option<AccelData>>>,
    pub latest_gyro: Arc<RwLock<Option<GyroData>>>,
    pub latest_gps: Arc<RwLock<Option<GpsData>>>,
    pub latest_mag: Arc<RwLock<Option<types::MagData>>>,
    pub latest_baro: Arc<RwLock<Option<types::BaroData>>>,
    pub accel_count: Arc<RwLock<u64>>,
    pub gyro_count: Arc<RwLock<u64>>,
    pub mag_count: Arc<RwLock<u64>>,
    pub baro_count: Arc<RwLock<u64>>,
    pub gps_count: Arc<RwLock<u64>>,
}

impl SensorState {
    fn new() -> Self {
        Self {
            accel_buffer: Arc::new(RwLock::new(VecDeque::with_capacity(1024))),
            gyro_buffer: Arc::new(RwLock::new(VecDeque::with_capacity(1024))),
            mag_buffer: Arc::new(RwLock::new(VecDeque::with_capacity(512))),
            baro_buffer: Arc::new(RwLock::new(VecDeque::with_capacity(256))),
            latest_accel: Arc::new(RwLock::new(None)),
            latest_gyro: Arc::new(RwLock::new(None)),
            latest_gps: Arc::new(RwLock::new(None)),
            latest_mag: Arc::new(RwLock::new(None)),
            latest_baro: Arc::new(RwLock::new(None)),
            accel_count: Arc::new(RwLock::new(0u64)),
            gyro_count: Arc::new(RwLock::new(0u64)),
            mag_count: Arc::new(RwLock::new(0u64)),
            baro_count: Arc::new(RwLock::new(0u64)),
            gps_count: Arc::new(RwLock::new(0u64)),
        }
    }
}

/// Combined sensor reader task: Read accel, gyro, and mag from single termux-sensor stream
/// Accel and gyro come from same LSM6DSO IMU, mag is AK09918; requested together
/// Handles multi-line pretty-printed JSON by accumulating until complete object
async fn imu_reader_task(
    state: SensorState,
    health_monitor: Arc<HealthMonitor>,
    enable_gyro: bool,
) {
    let sensor_list = if enable_gyro {
        "Accelerometer,Gyroscope,Magnetometer,Pressure"
    } else {
        "Accelerometer,Magnetometer,Pressure"
    };
    eprintln!(
        "[imu-reader] Initializing IMU reader (sensors: {})",
        sensor_list
    );

    // Cleanup sensor
    let _ = Command::new("termux-sensor").arg("-c").output().await;
    sleep(Duration::from_millis(500)).await;

    // Single termux-sensor command for accel, gyro, mag (no jq - handle JSON in Rust)
    let mut child = match Command::new("termux-sensor")
        .arg("-s")
        .arg(sensor_list)
        .arg("-d")
        .arg("20")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
    {
        Ok(p) => {
            eprintln!("[imu-reader] termux-sensor spawned");
            p
        }
        Err(e) => {
            eprintln!("[imu-reader] Failed to spawn termux-sensor: {}", e);
            return;
        }
    };

    let stdout = match child.stdout.take() {
        Some(s) => s,
        None => {
            eprintln!("[imu-reader] No stdout");
            return;
        }
    };

    let stderr = match child.stderr.take() {
        Some(s) => s,
        None => {
            eprintln!("[imu-reader] No stderr");
            return;
        }
    };

    // Spawn background task to log any errors
    tokio::spawn(async move {
        let reader = BufReader::new(stderr);
        let mut lines = AsyncBufReadExt::lines(reader);
        while let Ok(Some(line)) = lines.next_line().await {
            eprintln!("[imu-reader STDERR]: {}", line);
        }
    });

    // Read lines and accumulate multi-line JSON objects
    let reader = BufReader::new(stdout);
    let mut lines = AsyncBufReadExt::lines(reader);
    let mut accel_count = 0u64;
    let mut gyro_count = 0u64;
    let mut json_buffer = String::new();
    let mut brace_depth = 0;

    eprintln!("[imu-reader] Starting combined accel+gyro read loop...");

    while let Ok(Some(line)) = lines.next_line().await {
        let trimmed = line.trim();

        // Count braces to detect complete JSON objects
        for ch in trimmed.chars() {
            if ch == '{' {
                brace_depth += 1;
            } else if ch == '}' {
                brace_depth -= 1;
            }
        }

        // Accumulate line
        if !json_buffer.is_empty() {
            json_buffer.push(' ');
        }
        json_buffer.push_str(trimmed);

        // Safety valve: drop malformed/too-large JSON to avoid unbounded growth
        if json_buffer.len() > 4096 {
            eprintln!(
                "[imu-reader] WARN: JSON buffer exceeded {} bytes, discarding partial object",
                json_buffer.len()
            );
            json_buffer.clear();
            brace_depth = 0;
            continue;
        }

        // When braces are balanced (and not zero), we have a complete object
        if brace_depth == 0 && !json_buffer.is_empty() && json_buffer.contains('{') {
            // Try to parse the accumulated JSON
            if let Ok(obj) = serde_json::from_str::<serde_json::Value>(&json_buffer) {
                if let Some(obj_map) = obj.as_object() {
                    // Process both accel and gyro from same JSON object
                    for (sensor_key, sensor_data) in obj_map.iter() {
                        if sensor_key.contains("Accelerometer") {
                            if let Some(values) =
                                sensor_data.get("values").and_then(|v| v.as_array())
                            {
                                if values.len() >= 3 {
                                    health_monitor.accel.update(); // Heartbeat
                                    let accel = AccelData {
                                        timestamp: Utc::now().timestamp_millis() as f64 / 1000.0,
                                        x: values[0].as_f64().unwrap_or(0.0),
                                        y: values[1].as_f64().unwrap_or(0.0),
                                        z: values[2].as_f64().unwrap_or(0.0),
                                    };

                                    {
                                        let mut buf = state.accel_buffer.write().await;
                                        if buf.len() > 1024 {
                                            buf.pop_front();
                                        }
                                        buf.push_back(accel.clone());
                                    }

                                    {
                                        let mut latest = state.latest_accel.write().await;
                                        *latest = Some(accel);
                                    }

                                    {
                                        let mut count = state.accel_count.write().await;
                                        *count += 1;
                                    }

                                    accel_count += 1;
                                }
                            }
                        } else if sensor_key.contains("Gyroscope") {
                            if let Some(values) =
                                sensor_data.get("values").and_then(|v| v.as_array())
                            {
                                if values.len() >= 3 {
                                    health_monitor.gyro.update(); // Heartbeat
                                    let gyro = GyroData {
                                        timestamp: Utc::now().timestamp_millis() as f64 / 1000.0,
                                        x: values[0].as_f64().unwrap_or(0.0),
                                        y: values[1].as_f64().unwrap_or(0.0),
                                        z: values[2].as_f64().unwrap_or(0.0),
                                    };

                                    {
                                        let mut buf = state.gyro_buffer.write().await;
                                        if buf.len() > 1024 {
                                            buf.pop_front();
                                        }
                                        buf.push_back(gyro.clone());
                                    }

                                    {
                                        let mut latest = state.latest_gyro.write().await;
                                        *latest = Some(gyro);
                                    }

                                    {
                                        let mut count = state.gyro_count.write().await;
                                        *count += 1;
                                    }

                                    gyro_count += 1;
                                }
                            }
                        } else if sensor_key.contains("Magnetometer") {
                            if let Some(values) =
                                sensor_data.get("values").and_then(|v| v.as_array())
                            {
                                if values.len() >= 3 {
                                    let mag = types::MagData {
                                        timestamp: Utc::now().timestamp_millis() as f64 / 1000.0,
                                        x: values[0].as_f64().unwrap_or(0.0),
                                        y: values[1].as_f64().unwrap_or(0.0),
                                        z: values[2].as_f64().unwrap_or(0.0),
                                    };
                                    {
                                        let mut buf = state.mag_buffer.write().await;
                                        if buf.len() > 512 {
                                            buf.pop_front();
                                        }
                                        buf.push_back(mag.clone());
                                    }
                                    {
                                        let mut latest = state.latest_mag.write().await;
                                        *latest = Some(mag);
                                    }
                                    {
                                        let mut count = state.mag_count.write().await;
                                        *count += 1;
                                    }
                                }
                            }
                        } else if sensor_key.contains("Pressure") {
                            if let Some(values) =
                                sensor_data.get("values").and_then(|v| v.as_array())
                            {
                                if let Some(p) = values.get(0).and_then(|v| v.as_f64()) {
                                    let baro = types::BaroData {
                                        timestamp: Utc::now().timestamp_millis() as f64 / 1000.0,
                                        pressure_hpa: p,
                                    };
                                    {
                                        let mut buf = state.baro_buffer.write().await;
                                        if buf.len() > 256 {
                                            buf.pop_front();
                                        }
                                        buf.push_back(baro.clone());
                                    }
                                    {
                                        let mut latest = state.latest_baro.write().await;
                                        *latest = Some(baro);
                                    }
                                    {
                                        let mut count = state.baro_count.write().await;
                                        *count += 1;
                                    }
                                }
                            }
                        }
                    }

                    // Log progress every 50 combined updates
                    if (accel_count + gyro_count) % 50 == 0 && (accel_count + gyro_count) > 0 {
                        eprintln!(
                            "[imu-reader] Accel: {}, Gyro: {} samples parsed",
                            accel_count, gyro_count
                        );
                    }
                }
            }

            // Clear buffer for next object
            json_buffer.clear();
        }
    }

    eprintln!(
        "[imu-reader] Stream ended: Accel: {}, Gyro: {}",
        accel_count, gyro_count
    );
}

/// GPS reader task: Poll termux-location every 1000ms
async fn gps_reader_task(state: SensorState, health_monitor: Arc<HealthMonitor>) {
    eprintln!("[gps-reader] Initializing GPS reader");
    let mut fix_count = 0u64;

    loop {
        sleep(Duration::from_millis(1000)).await;

        // Call termux-location
        match Command::new("termux-location")
            .arg("-p")
            .arg("gps")
            .output()
            .await
        {
            Ok(output) => {
                let stdout = String::from_utf8_lossy(&output.stdout);
                if let Ok(gps_json) = serde_json::from_str::<serde_json::Value>(&stdout) {
                    if let Some(obj) = gps_json.as_object() {
                        if let (Some(lat), Some(lon), Some(speed), Some(bearing), Some(accuracy)) = (
                            obj.get("latitude").and_then(|v| v.as_f64()),
                            obj.get("longitude").and_then(|v| v.as_f64()),
                            obj.get("speed").and_then(|v| v.as_f64()),
                            obj.get("bearing").and_then(|v| v.as_f64()),
                            obj.get("accuracy").and_then(|v| v.as_f64()),
                        ) {
                            health_monitor.gps.update(); // Heartbeat

                            let gps_data = GpsData {
                                timestamp: Utc::now().timestamp_millis() as f64 / 1000.0,
                                latitude: lat,
                                longitude: lon,
                                speed,
                                bearing,
                                accuracy,
                            };

                            {
                                let mut latest = state.latest_gps.write().await;
                                *latest = Some(gps_data);
                            }

                            {
                                let mut count = state.gps_count.write().await;
                                *count += 1;
                            }

                            fix_count += 1;
                            if fix_count % 10 == 0 {
                                eprintln!(
                                    "[gps-reader] Fix {}: ({:.5}, {:.5}) speed={:.2} m/s bearing={:.1}° acc={:.1}m",
                                    fix_count, lat, lon, speed, bearing, accuracy
                                );
                            }
                        }
                    }
                }
            }
            Err(e) => {
                eprintln!("[gps-reader] Error: {}", e);
            }
        }
    }
}

use health_monitor::HealthMonitor;
use restart_manager::RestartManager;

/// Build track path from GPS readings with >5m distance downsampling
fn build_track_path(readings: &[SensorReading]) -> Vec<[f64; 2]> {
    let mut track_path = Vec::new();
    let mut last_point: Option<[f64; 2]> = None;

    for reading in readings {
        if let Some(gps) = &reading.gps {
            let current_point = [gps.latitude, gps.longitude];

            if let Some(last) = last_point {
                let dist_sq =
                    (current_point[0] - last[0]).powi(2) + (current_point[1] - last[1]).powi(2);
                // ~5 meters in degrees at equator
                let threshold_sq = 0.00005_f64; // (0.0000447)^2 ≈ 5m
                if dist_sq >= threshold_sq {
                    track_path.push(current_point);
                    last_point = Some(current_point);
                }
            } else {
                track_path.push(current_point);
                last_point = Some(current_point);
            }
        }
    }
    track_path
}

/// Append a SensorReading as JSONL to the session logger (if enabled)
fn log_jsonl_reading(
    logger: &mut Option<GzEncoder<BufWriter<File>>>,
    reading: &SensorReading,
    counter: &mut usize,
) -> Result<()> {
    if let Some(enc) = logger.as_mut() {
        let line = serde_json::to_string(reading)?;
        enc.write_all(line.as_bytes())?;
        enc.write_all(b"\n")?;
        *counter += 1;
        if *counter % 500 == 0 {
            enc.flush()?; // keep buffered JSONL from growing without bound
        }
    }
    Ok(())
}

/// Save JSON with gzip compression, returning the actual filename written
fn save_json_compressed(
    output: &ComparisonOutput,
    output_dir: &str,
    session_id: &str,
) -> Result<String> {
    let session_path = format!("{}/comparison_{}.json.gz", output_dir, session_id);
    let temp_path = format!("{}.tmp", session_path);

    // Serialize to JSON
    let json = serde_json::to_string_pretty(&output)?;

    // Write to temp file with gzip compression
    {
        let file = File::create(&temp_path)?;
        let mut encoder = GzEncoder::new(file, Compression::default());
        encoder.write_all(json.as_bytes())?;
        encoder.finish()?;
    }

    // Atomic rename: move temp -> final
    std::fs::rename(&temp_path, &session_path)?;

    Ok(session_path)
}

/// Handle fusion events — log, record incidents, etc.
fn handle_fusion_events(
    events: &[FusionEvent],
    rerun_logger: &Option<RerunLogger>,
    incidents: &mut Vec<incident::Incident>,
) {
    for event in events {
        match event {
            FusionEvent::IncidentDetected(incident) => {
                eprintln!(
                    "[INCIDENT] {} Detected: {:.1} (Unit)",
                    incident.incident_type, incident.magnitude
                );
                if let Some(ref logger) = rerun_logger {
                    if let (Some(lat), Some(lon)) = (incident.latitude, incident.longitude) {
                        logger.set_time(incident.timestamp);
                        logger.log_incident(
                            &incident.incident_type,
                            incident.magnitude,
                            lat,
                            lon,
                        );
                    }
                }
                incidents.push(incident.clone());
            }
            FusionEvent::SpeedClamped { from_speed, to_limit, gap_secs } => {
                eprintln!(
                    "[CLAMP] gap={:.1}s speed {:.1} -> limit {:.1}",
                    gap_secs, from_speed, to_limit
                );
            }
            FusionEvent::GapClampActive { gap_secs, speed, limit } => {
                eprintln!(
                    "[GAP CLAMP] gap={:.1}s speed {:.1} -> limit {:.1}",
                    gap_secs, speed, limit
                );
            }
            FusionEvent::GpsRejected { accuracy, speed } => {
                eprintln!(
                    "[GPS] Rejected fix (acc={:.1}m, speed={:.2}m/s) as outlier",
                    accuracy, speed
                );
            }
            FusionEvent::ColdStartInitialized { lat, lon } => {
                println!(
                    "[COLD START] GPS Locked. Origin: ({:.6}, {:.6}). EKF initialized at REST.",
                    lat, lon
                );
                println!("[COLD START] Skipping first GPS update to prevent initialization shock.");
            }
            FusionEvent::HeadingAligned { bearing_deg, yaw_deg, speed } => {
                eprintln!(
                    "[ALIGN] Heading aligned to GPS: bearing {:.1}° -> yaw {:.1}° (speed: {:.2} m/s)",
                    bearing_deg, yaw_deg, speed
                );
            }
            FusionEvent::HighGpsLatency { latency_secs } => {
                eprintln!("[GPS] High latency: {:.2}s", latency_secs);
            }
            FusionEvent::NhcSkipped { gap_secs } => {
                eprintln!("[NHC SKIP] gap {:.1}s", gap_secs);
            }
            FusionEvent::MagCorrection { gap_secs, innovation_deg } => {
                eprintln!(
                    "[MAG] gap {:.1}s yaw correction: {:.1}°",
                    gap_secs, innovation_deg
                );
            }
            FusionEvent::GravityRefined { refinement_count, estimate, magnitude, drift } => {
                eprintln!(
                    "[CALIB-DYN] Refinement #{}: gravity ({:.3}, {:.3}, {:.3}) mag={:.3} drift={:.3}m/s²",
                    refinement_count, estimate.0, estimate.1, estimate.2, magnitude, drift
                );
            }
            FusionEvent::GravityDriftWarning { drift, threshold } => {
                eprintln!(
                    "[CALIB-DYN] WARNING: Gravity drift {:.3}m/s² exceeds threshold {:.3}m/s² - possible sensor degradation",
                    drift, threshold
                );
            }
            FusionEvent::FgoOptimization { nodes, gps_factors, iteration } => {
                eprintln!(
                    "[FGO] Optimization #{}: {} nodes, {} GPS factors",
                    iteration, nodes, gps_factors
                );
            }
            FusionEvent::ZuptApplied => {}
            FusionEvent::GapModeExited => {}
        }
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    // Install panic hook
    let original_hook = panic::take_hook();
    panic::set_hook(Box::new(move |panic_info| {
        let msg = if let Some(s) = panic_info.payload().downcast_ref::<&str>() {
            s.to_string()
        } else if let Some(s) = panic_info.payload().downcast_ref::<String>() {
            s.clone()
        } else {
            "Unknown panic".to_string()
        };

        let location = panic_info
            .location()
            .map(|l| format!("{}:{}", l.file(), l.line()))
            .unwrap_or_else(|| "unknown location".to_string());

        debug_log(&format!("PANIC: {} at {}", msg, location));
        eprintln!("PANIC: {} at {}", msg, location);

        original_hook(panic_info);
    }));

    let args = Args::parse();

    println!("[{}] Motion Tracker RS Starting", ts_now());
    println!("  Duration: {} seconds (0=continuous)", args.duration);
    println!("  Enable Gyro: {}", args.enable_gyro);
    println!("  Filter Mode: {}", args.filter);
    println!("  Output Dir: {}", args.output_dir);

    std::fs::create_dir_all(&args.output_dir)?;

    // Single-session identifiers/paths
    let session_id = ts_now_clean();
    let session_json_path = format!("{}/session_{}.jsonl.gz", args.output_dir, session_id);
    let session_json_file = File::create(&session_json_path)?;
    let session_json_writer = BufWriter::new(session_json_file);
    let mut session_logger = Some(GzEncoder::new(session_json_writer, Compression::fast()));
    let mut jsonl_count: usize = 0;
    println!(
        "[{}] JSONL logging to {} (one file per session)",
        ts_now(),
        session_json_path
    );

    // Shared sensor state
    let sensor_state = SensorState::new();

    // Initialize Health Monitor & Restart Manager
    let health_monitor = Arc::new(HealthMonitor::new());
    let restart_manager = Arc::new(RestartManager::new());

    // Spawn Dashboard Task
    let dashboard_state = sensor_state.clone();
    let dashboard_port = args.dashboard_port;
    tokio::spawn(async move {
        dashboard::start_dashboard(dashboard_state, dashboard_port).await;
    });

    // Spawn Health Monitor Task
    let hm_clone = health_monitor.clone();
    let rm_clone = restart_manager.clone();
    tokio::spawn(async move {
        health_monitor::health_monitor_task(hm_clone, rm_clone).await;
    });

    // Spawn combined IMU reader task (accel + gyro from single termux-sensor stream)
    let imu_state = sensor_state.clone();
    let imu_hm = health_monitor.clone();
    let imu_rm = restart_manager.clone();
    let enable_gyro_clone = args.enable_gyro;
    let imu_reader_handle = tokio::spawn(async move {
        // Supervisor loop
        loop {
            if imu_rm.accel_circuit_tripped() || imu_rm.gyro_circuit_tripped() {
                eprintln!(
                    "[SUPERVISOR] IMU circuit breaker tripped; exiting to avoid restart loop."
                );
                std::process::exit(2);
            }

            // Check if we can start/restart
            let can_run = imu_rm.accel_ready_restart(); // Using accel as proxy for shared IMU

            if can_run {
                eprintln!("[SUPERVISOR] Starting IMU task...");
                // Run the task - if it returns, it failed or finished
                imu_reader_task(imu_state.clone(), imu_hm.clone(), enable_gyro_clone).await;

                // If task exits, report failure
                eprintln!("[SUPERVISOR] IMU task exited unexpectedly.");
                imu_rm.accel_restart_failed(); // Record failure to trigger backoff
                imu_rm.gyro_restart_failed();

                if imu_rm.accel_circuit_tripped() || imu_rm.gyro_circuit_tripped() {
                    eprintln!("[SUPERVISOR] IMU circuit breaker tripped after repeated failures; exiting.");
                    std::process::exit(2);
                }
            } else {
                // Backoff wait
                sleep(Duration::from_millis(100)).await;
            }
        }
    });

    // Spawn GPS reader task (polls termux-location every 1000ms)
    let gps_state = sensor_state.clone();
    let gps_hm = health_monitor.clone();
    let gps_rm = restart_manager.clone();
    let gps_reader_handle = tokio::spawn(async move {
        loop {
            if gps_rm.gps_circuit_tripped() {
                eprintln!(
                    "[SUPERVISOR] GPS circuit breaker tripped; exiting to avoid restart loop."
                );
                std::process::exit(2);
            }

            let can_run = gps_rm.gps_ready_restart();

            if can_run {
                eprintln!("[SUPERVISOR] Starting GPS task...");
                gps_reader_task(gps_state.clone(), gps_hm.clone()).await;

                eprintln!("[SUPERVISOR] GPS task exited unexpectedly.");
                gps_rm.gps_restart_failed();

                if gps_rm.gps_circuit_tripped() {
                    eprintln!("[SUPERVISOR] GPS circuit breaker tripped after repeated failures; exiting.");
                    std::process::exit(2);
                }
            } else {
                sleep(Duration::from_millis(100)).await;
            }
        }
    });

    // ===== Initialize SensorFusion =====
    let config = FusionConfig {
        enable_mag: args.enable_mag,
        enable_baro: args.enable_baro,
        enable_gyro: args.enable_gyro,
        enable_complementary: args.filter == "complementary" || args.filter == "both",
        ..FusionConfig::default()
    };
    let mut fusion = SensorFusion::new(config);

    let mut incidents: Vec<incident::Incident> = Vec::new();
    let mut readings: Vec<SensorReading> = Vec::new();
    let mut trajectories: Vec<TrajectoryPoint> = Vec::new();
    let mut covariance_snapshots: Vec<CovarianceSnapshot> = Vec::new();

    let mut peak_memory_mb: f64 = 0.0;
    let mut current_memory_mb: f64 = 0.0;

    let start = Utc::now();
    let mut last_save = Utc::now();
    let mut last_status_update = Utc::now();

    // ===== STARTUP CALIBRATION PREAMBLE =====
    println!("[{}] Starting sensor calibration...", ts_now());
    eprintln!("[CALIB] Waiting 3 seconds for sensor data to arrive...");
    sleep(Duration::from_secs(3)).await;

    // Calculate gravity bias and gyro bias from buffer samples with generous retry logic
    let calibration_complete = {
        let accel_buf = sensor_state.accel_buffer.read().await;
        let gyro_buf = sensor_state.gyro_buffer.read().await;
        eprintln!(
            "[CALIB] After 3s: {} accel samples, {} gyro samples",
            accel_buf.len(),
            gyro_buf.len()
        );

        if accel_buf.len() < 50 {
            eprintln!(
                "[CALIB] WARNING: Only {} accel samples. Waiting 2 more seconds...",
                accel_buf.len()
            );
            drop(accel_buf);
            drop(gyro_buf);
            sleep(Duration::from_secs(2)).await;

            let accel_buf = sensor_state.accel_buffer.read().await;
            let gyro_buf = sensor_state.gyro_buffer.read().await;
            eprintln!(
                "[CALIB] After 5s: {} accel samples, {} gyro samples",
                accel_buf.len(),
                gyro_buf.len()
            );

            if accel_buf.len() < 50 {
                eprintln!(
                    "[CALIB] WARNING: Still only {} samples. Waiting 2 more seconds...",
                    accel_buf.len()
                );
                drop(accel_buf);
                drop(gyro_buf);
                sleep(Duration::from_secs(2)).await;

                let accel_buf = sensor_state.accel_buffer.read().await;
                let gyro_buf = sensor_state.gyro_buffer.read().await;
                eprintln!(
                    "[CALIB] After 7s: {} accel samples, {} gyro samples",
                    accel_buf.len(),
                    gyro_buf.len()
                );

                if accel_buf.len() < 50 {
                    eprintln!(
                        "[CALIB] FAILED: Still only {} samples after 7 seconds. Using defaults.",
                        accel_buf.len()
                    );
                    fusion.set_biases((0.0, 0.0, 9.81), (0.0, 0.0, 0.0));
                    false
                } else {
                    fusion.set_calibration(&accel_buf, &gyro_buf)
                }
            } else {
                fusion.set_calibration(&accel_buf, &gyro_buf)
            }
        } else {
            fusion.set_calibration(&accel_buf, &gyro_buf)
        }
    };

    {
        let snap = fusion.get_snapshot();
        eprintln!(
            "[CALIB] Gravity bias vector: ({:.3}, {:.3}, {:.3}) m/s²",
            snap.gravity_bias.0, snap.gravity_bias.1, snap.gravity_bias.2
        );
        eprintln!(
            "[CALIB] Gyro bias vector: ({:.6}, {:.6}, {:.6}) rad/s",
            snap.gyro_bias.0, snap.gyro_bias.1, snap.gyro_bias.2
        );
        eprintln!("[CALIB] Calibration complete: {}", calibration_complete);
        eprintln!("[CALIB-DYN] Dynamic calibration initialized, will refine gravity during stillness");
        eprintln!("[FGO] Factor Graph Optimizer initialized (shadow mode)");
    }

    // Duration timeout
    let (duration_tx, mut duration_rx) = mpsc::channel::<()>(1);
    let _duration_handle = if args.duration > 0 {
        let tx = duration_tx.clone();
        let duration_secs = args.duration;
        Some(tokio::spawn(async move {
            sleep(Duration::from_secs(duration_secs)).await;
            eprintln!(
                "[TIMEOUT] Duration timer fired after {} seconds",
                duration_secs
            );
            let _ = tx.send(()).await;
        }))
    } else {
        None
    };

    println!("[{}] Starting data collection...", ts_now());

    // Initialize Rerun logger for 3D visualization (v0.15 API compatible)
    let rerun_output_path = format!(
        "motion_tracker_sessions/rerun_{}.rrd",
        start.format("%Y%m%d_%H%M%S")
    );
    let rerun_logger = match RerunLogger::new(&rerun_output_path) {
        Ok(logger) => {
            eprintln!("[RERUN] Logging enabled → {}", rerun_output_path);
            Some(logger)
        }
        Err(e) => {
            eprintln!("[RERUN] WARNING: Failed to initialize Rerun logger: {}", e);
            None
        }
    };

    // Main loop: Consumer at fixed 20ms tick (50Hz)
    loop {
        // Check duration
        if duration_rx.try_recv().is_ok() {
            println!("[{}] Duration reached, stopping...", ts_now());
            break;
        }

        // Poll for keyboard input ('k' for virtual kick)
        if crossterm::event::poll(std::time::Duration::ZERO).unwrap_or(false) {
            if let Ok(crossterm::event::Event::Key(key_event)) = crossterm::event::read() {
                if key_event.code == crossterm::event::KeyCode::Char('k') {
                    eprintln!("[KICK] Virtual acceleration triggered (10 frames)");
                    fusion.trigger_kick(10);
                }
            }
        }

        // Cache mag/baro from async locks for fusion
        if let Some(mag) = sensor_state.latest_mag.read().await.as_ref() {
            fusion.feed_mag(mag);
        }
        if let Some(baro) = sensor_state.latest_baro.read().await.as_ref() {
            fusion.feed_baro(baro);
        }

        // Drain accel buffer
        {
            let gps_snapshot = {
                let g = sensor_state.latest_gps.read().await;
                g.clone()
            };
            let mut buf = sensor_state.accel_buffer.write().await;
            while let Some(accel) = buf.pop_front() {
                let events = fusion.feed_accel(&accel);
                handle_fusion_events(&events, &rerun_logger, &mut incidents);

                let snap = fusion.get_snapshot();

                // Simple specific power estimate (display metric, stays in main.rs)
                let speed_for_power = gps_snapshot
                    .as_ref()
                    .map(|g| g.speed)
                    .or_else(|| snap.comp_state.as_ref().map(|c| c.velocity))
                    .or_else(|| snap.es_ekf_state.as_ref().map(|s| s.velocity))
                    .unwrap_or(0.0);
                let accel_mag = (accel.x * accel.x + accel.y * accel.y + accel.z * accel.z).sqrt();
                let forward_accel_approx = (accel_mag - 9.81).abs();
                let specific_power_est = if speed_for_power > 1.0 {
                    forward_accel_approx * speed_for_power
                } else {
                    0.0
                };

                let reading = SensorReading {
                    timestamp: accel.timestamp,
                    accel: Some(accel.clone()),
                    gyro: None,
                    gps: None,
                    baro: sensor_state.latest_baro.read().await.clone(),
                    roughness: Some(snap.roughness),
                    specific_power_w_per_kg: specific_power_est,
                    power_coefficient: 0.0,
                    experimental_13d: snap.ekf_13d_state.clone(),
                    experimental_15d: Some(snap.ekf_15d_state.clone()),
                    mag: sensor_state.latest_mag.read().await.clone(),
                    fgo: snap.fgo_state.clone(),
                };

                log_jsonl_reading(&mut session_logger, &reading, &mut jsonl_count)?;
                readings.push(reading);

                // Rerun logging: accel data
                if let Some(ref logger) = rerun_logger {
                    logger.set_time(accel.timestamp);
                    logger.log_accel_raw(accel.x, accel.y, accel.z);
                    // Log corrected accel (gravity-subtracted)
                    let grav = snap.gravity_bias;
                    logger.log_accel_filtered(accel.x - grav.0, accel.y - grav.1, accel.z - grav.2);
                }
            }
        }

        // Drain gyro buffer
        {
            let mut buf = sensor_state.gyro_buffer.write().await;
            while let Some(gyro) = buf.pop_front() {
                let events = fusion.feed_gyro(&gyro);
                handle_fusion_events(&events, &rerun_logger, &mut incidents);

                // Attach gyro to last reading and update 15D state
                if let Some(last) = readings.last_mut() {
                    last.gyro = Some(gyro.clone());
                    let snap = fusion.get_snapshot();
                    last.experimental_13d = snap.ekf_13d_state.clone();
                    last.experimental_15d = Some(snap.ekf_15d_state.clone());
                }

                // Rerun logging: gyro data
                if let Some(ref logger) = rerun_logger {
                    logger.set_time(gyro.timestamp);
                    logger.log_gyro_raw(gyro.x, gyro.y, gyro.z);
                }
            }
        }

        // GPS integration
        {
            let latest_gps = sensor_state.latest_gps.read().await;
            if let Some(gps) = latest_gps.as_ref() {
                let system_now = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap_or_default()
                    .as_secs_f64();
                let events = fusion.feed_gps(gps, system_now);
                handle_fusion_events(&events, &rerun_logger, &mut incidents);

                // Record GPS reading if it was accepted (check if it's a new fix)
                if events.iter().any(|e| !matches!(e, FusionEvent::GpsRejected { .. })) {
                    let snap = fusion.get_snapshot();
                    let gps_reading = SensorReading {
                        timestamp: gps.timestamp,
                        accel: None,
                        gyro: None,
                        mag: sensor_state.latest_mag.read().await.clone(),
                        baro: sensor_state.latest_baro.read().await.clone(),
                        gps: Some(gps.clone()),
                        roughness: None,
                        specific_power_w_per_kg: 0.0,
                        power_coefficient: 0.0,
                        experimental_13d: snap.ekf_13d_state.clone(),
                        experimental_15d: Some(snap.ekf_15d_state.clone()),
                        fgo: snap.fgo_state.clone(),
                    };
                    log_jsonl_reading(&mut session_logger, &gps_reading, &mut jsonl_count)?;
                    readings.push(gps_reading);
                }
            }
        }

        // ZUPT + gravity refinement + EsEKF predict
        {
            let events = fusion.tick();
            handle_fusion_events(&events, &rerun_logger, &mut incidents);
        }

        // Rerun logging: filter states
        if let Some(ref logger) = rerun_logger {
            let elapsed =
                Utc::now().signed_duration_since(start).num_milliseconds() as f64 / 1000.0;
            logger.set_time(elapsed);

            let snap = fusion.get_snapshot();

            // Log EKF state
            if let Some(ref ekf_state) = snap.es_ekf_state {
                logger.log_ekf_velocity(
                    ekf_state.velocity_vector.0,
                    ekf_state.velocity_vector.1,
                    0.0,
                );
            }

            // Log 13D filter state
            if let Some(ref ekf_13d_state) = snap.ekf_13d_state {
                logger.log_13d_state(
                    ekf_13d_state.position.0,
                    ekf_13d_state.position.1,
                    ekf_13d_state.position.2,
                    ekf_13d_state.velocity.0,
                    ekf_13d_state.velocity.1,
                    ekf_13d_state.velocity.2,
                    ekf_13d_state.quaternion.0,
                    ekf_13d_state.quaternion.1,
                    ekf_13d_state.quaternion.2,
                    ekf_13d_state.quaternion.3,
                );

                logger.log_orientation(
                    ekf_13d_state.quaternion.0,
                    ekf_13d_state.quaternion.1,
                    ekf_13d_state.quaternion.2,
                    ekf_13d_state.quaternion.3,
                );

                logger.log_position(
                    ekf_13d_state.position.0,
                    ekf_13d_state.position.1,
                    ekf_13d_state.position.2,
                );

                let ekf_speed = snap.es_ekf_state.as_ref().map(|s| s.velocity).unwrap_or(0.0);
                let ekf_13d_speed = (ekf_13d_state.velocity.0 * ekf_13d_state.velocity.0
                    + ekf_13d_state.velocity.1 * ekf_13d_state.velocity.1
                    + ekf_13d_state.velocity.2 * ekf_13d_state.velocity.2)
                    .sqrt();
                logger.log_filter_comparison("velocity", ekf_speed, ekf_13d_speed);
                logger.log_filter_comparison("position_x", 0.0, ekf_13d_state.position.0);
                logger.log_filter_comparison("position_y", 0.0, ekf_13d_state.position.1);
            }
        }

        // Status update every 2 seconds
        let now = Utc::now();
        if (now.signed_duration_since(last_status_update).num_seconds() as i64) >= 2i64 {
            let accel_count = *sensor_state.accel_count.read().await;
            let gyro_count = *sensor_state.gyro_count.read().await;
            let gps_count = *sensor_state.gps_count.read().await;

            let snap = fusion.get_snapshot();
            let uptime = now.signed_duration_since(start).num_seconds().max(0) as u64;

            let mut live_status = live_status::LiveStatus::new();
            live_status.timestamp = live_status::current_timestamp();
            live_status.accel_samples = accel_count;
            live_status.gyro_samples = gyro_count;
            live_status.gps_fixes = gps_count;
            live_status.incidents_detected = incidents.len() as u64;
            live_status.calibration_complete = calibration_complete;

            // Populate health monitoring status from health monitor
            let health_report = health_monitor.check_health();
            live_status.accel_healthy = health_report.accel_healthy;
            live_status.gyro_healthy = health_report.gyro_healthy;
            live_status.gps_healthy = health_report.gps_healthy;
            live_status.accel_silence_duration_secs = health_report
                .accel_silence_duration
                .map(|d| d.as_secs_f64())
                .unwrap_or(0.0);
            live_status.gyro_silence_duration_secs = health_report
                .gyro_silence_duration
                .map(|d| d.as_secs_f64())
                .unwrap_or(0.0);
            live_status.gps_silence_duration_secs = health_report
                .gps_silence_duration
                .map(|d| d.as_secs_f64())
                .unwrap_or(0.0);

            // Populate restart tracking from restart manager
            live_status.accel_restart_count = health_report.accel_restart_count;
            live_status.gyro_restart_count = health_report.gyro_restart_count;
            live_status.gps_restart_count = health_report.gps_restart_count;
            live_status.accel_can_restart = health_report.accel_can_restart;
            live_status.gps_can_restart = health_report.gps_can_restart;

            // Populate circuit breaker status
            live_status.circuit_breaker_tripped = restart_manager.any_circuit_tripped();
            live_status.circuit_breaker_since_secs = if live_status.circuit_breaker_tripped {
                uptime as f64
            } else {
                0.0
            };

            // Populate GPS data from latest fix
            if let Some(gps) = sensor_state.latest_gps.read().await.as_ref() {
                live_status.gps_speed = gps.speed;
                live_status.gps_bearing = gps.bearing;
                live_status.gps_accuracy = gps.accuracy;
                live_status.gps_lat = gps.latitude;
                live_status.gps_lon = gps.longitude;
                live_status.gps_healthy = true;

                // Log GPS ground truth to Rerun visualization
                if let Some(ref logger) = rerun_logger {
                    logger.set_time(gps.timestamp);
                    logger.log_gps(gps.latitude, gps.longitude, 0.0, gps.speed);
                }

                // Calculate virtual dyno specific power
                if let Some(accel) = sensor_state.latest_accel.read().await.as_ref() {
                    let calc_velocity = if gps.speed > 0.1 {
                        gps.speed
                    } else {
                        snap.comp_state.as_ref().map(|c| c.velocity).unwrap_or(0.0)
                    };
                    let accel_mag =
                        (accel.x * accel.x + accel.y * accel.y + accel.z * accel.z).sqrt();
                    let forward_accel_approx = (accel_mag - 9.81).abs();
                    let specific_power = if calc_velocity > 1.0 {
                        forward_accel_approx * calc_velocity
                    } else {
                        0.0
                    };
                    live_status.specific_power_w_per_kg = (specific_power * 100.0).round() / 100.0;
                    live_status.power_coefficient = 0.0;
                }
            }

            // Calculate magnitude of calibrated gravity vector
            let gravity_mag = (snap.gravity_bias.0 * snap.gravity_bias.0
                + snap.gravity_bias.1 * snap.gravity_bias.1
                + snap.gravity_bias.2 * snap.gravity_bias.2)
                .sqrt();
            live_status.gravity_magnitude = gravity_mag;
            live_status.uptime_seconds = uptime;

            if let Some(ref ekf_state) = snap.es_ekf_state {
                live_status.ekf_velocity = ekf_state.velocity;
                live_status.ekf_distance = ekf_state.distance;
                live_status.ekf_heading_deg = ekf_state.heading_deg;

                trajectories.push(TrajectoryPoint {
                    timestamp: live_status::current_timestamp(),
                    ekf_x: ekf_state.position_local.0,
                    ekf_y: ekf_state.position_local.1,
                    ekf_velocity: ekf_state.velocity,
                    ekf_heading_deg: ekf_state.heading_deg,
                    comp_velocity: snap.comp_state.as_ref().map(|c| c.velocity).unwrap_or(0.0),
                });

                let (trace, diag) = fusion.get_covariance_snapshot();
                covariance_snapshots.push(CovarianceSnapshot {
                    timestamp: live_status::current_timestamp(),
                    trace,
                    p00: diag[0],
                    p11: diag[1],
                    p22: diag[2],
                    p33: diag[3],
                    p44: diag[4],
                    p55: diag[5],
                    p66: diag[6],
                    p77: diag[7],
                });
            }

            if let Some(ref comp) = snap.comp_state {
                live_status.comp_velocity = comp.velocity;
            }

            current_memory_mb = get_memory_mb();
            peak_memory_mb = peak_memory_mb.max(current_memory_mb);

            let status_path = format!("{}/live_status.json", args.output_dir);
            let _ = live_status.save(&status_path);

            eprintln!(
                "[STATUS] Accel: {}, Gyro: {}, Mem: {:.1}MB",
                accel_count, gyro_count, current_memory_mb
            );

            last_status_update = now;
        }

        // Auto-save every 15 seconds
        if (now.signed_duration_since(last_save).num_seconds() as i64) >= 15i64 {
            let accel_count = *sensor_state.accel_count.read().await;
            let elapsed_secs = now.signed_duration_since(start).num_seconds().max(0i64) as u64;
            let gyro_count = *sensor_state.gyro_count.read().await;
            let gps_count = *sensor_state.gps_count.read().await;

            let snap = fusion.get_snapshot();
            let track_path = build_track_path(&readings);
            let output = ComparisonOutput {
                readings: readings.clone(),
                incidents: incidents.clone(),
                trajectories: trajectories.clone(),
                stats: Stats {
                    total_samples: readings.len(),
                    total_incidents: incidents.len(),
                    ekf_velocity: snap.es_ekf_state.as_ref().map(|s| s.velocity).unwrap_or(0.0),
                    ekf_distance: snap.es_ekf_state.as_ref().map(|s| s.distance).unwrap_or(0.0),
                    gps_fixes: gps_count,
                },
                metrics: Metrics {
                    test_duration_seconds: elapsed_secs,
                    accel_samples: accel_count,
                    gyro_samples: gyro_count,
                    gps_samples: gps_count,
                    gravity_magnitude: (snap.gravity_bias.0 * snap.gravity_bias.0
                        + snap.gravity_bias.1 * snap.gravity_bias.1
                        + snap.gravity_bias.2 * snap.gravity_bias.2)
                        .sqrt(),
                    gravity_x: snap.gravity_bias.0,
                    gravity_y: snap.gravity_bias.1,
                    gravity_z: snap.gravity_bias.2,
                    gyro_bias_x: snap.gyro_bias.0,
                    gyro_bias_y: snap.gyro_bias.1,
                    gyro_bias_z: snap.gyro_bias.2,
                    calibration_complete,
                    gravity_refinements: snap.gravity_refinements,
                    gravity_drift_magnitude: snap.gravity_drift,
                    gravity_final_x: snap.gravity_bias.0,
                    gravity_final_y: snap.gravity_bias.1,
                    gravity_final_z: snap.gravity_bias.2,
                    peak_memory_mb,
                    current_memory_mb,
                    covariance_snapshots: covariance_snapshots.clone(),
                },
                system_health: restart_manager.status_report(),
                track_path,
            };

            let filename = save_json_compressed(&output, &args.output_dir, &session_id)?;

            println!(
                "[{}] Auto-saved {} samples to {}",
                ts_now(),
                readings.len(),
                filename
            );

            // Prune historical IMU readings to cap memory (retain GPS and recent IMU for dashboard)
            let cutoff_time = live_status::current_timestamp() - 60.0;
            readings.retain(|r| r.gps.is_some() || r.timestamp > cutoff_time);

            last_save = now;
        }

        // Consumer tick: 20ms (50Hz)
        sleep(Duration::from_millis(20)).await;
    }

    // Final drain of remaining data in buffers BEFORE aborting readers
    eprintln!("[CLEANUP] Draining remaining sensor data...");
    loop {
        // Drain accel buffer
        let accel_drained = {
            let mut buf = sensor_state.accel_buffer.write().await;
            let mut count = 0;
            while let Some(accel) = buf.pop_front() {
                let events = fusion.feed_accel(&accel);
                handle_fusion_events(&events, &rerun_logger, &mut incidents);

                let snap = fusion.get_snapshot();
                let reading = SensorReading {
                    timestamp: accel.timestamp,
                    accel: Some(accel.clone()),
                    gyro: None,
                    mag: sensor_state.latest_mag.read().await.clone(),
                    baro: sensor_state.latest_baro.read().await.clone(),
                    gps: None,
                    roughness: Some(snap.roughness),
                    specific_power_w_per_kg: 0.0,
                    power_coefficient: 0.0,
                    experimental_13d: None,
                    experimental_15d: None,
                    fgo: None,
                };

                log_jsonl_reading(&mut session_logger, &reading, &mut jsonl_count)?;
                readings.push(reading);
                count += 1;
            }
            count
        };

        // Drain gyro buffer
        let gyro_drained = {
            let mut buf = sensor_state.gyro_buffer.write().await;
            let mut count = 0;
            while let Some(gyro) = buf.pop_front() {
                let events = fusion.feed_gyro(&gyro);
                handle_fusion_events(&events, &rerun_logger, &mut incidents);

                if let Some(last) = readings.last_mut() {
                    last.gyro = Some(gyro.clone());
                }
                count += 1;
            }
            count
        };

        // If both buffers are empty, we're done draining
        if accel_drained == 0 && gyro_drained == 0 {
            break;
        }

        // Small sleep to allow more data to arrive before final drain check
        sleep(Duration::from_millis(10)).await;
    }

    eprintln!(
        "[CLEANUP] Final drain complete: {} readings collected",
        readings.len()
    );

    // Abort IMU and GPS reader tasks
    println!("[CLEANUP] Aborting IMU reader task...");
    imu_reader_handle.abort();
    println!("[CLEANUP] Aborting GPS reader task...");
    gps_reader_handle.abort();
    tokio::task::yield_now().await;

    // Final stillness clamp
    if fusion.is_stationary() {
        let events = fusion.tick();
        handle_fusion_events(&events, &rerun_logger, &mut incidents);
    }

    // Final save
    let accel_count = *sensor_state.accel_count.read().await;
    let gyro_count = *sensor_state.gyro_count.read().await;
    let gps_count = *sensor_state.gps_count.read().await;
    let snap = fusion.get_snapshot();
    let uptime = Utc::now().signed_duration_since(start).num_seconds().max(0) as u64;

    let track_path = build_track_path(&readings);
    let output = ComparisonOutput {
        readings: readings.clone(),
        incidents: incidents.clone(),
        trajectories: trajectories.clone(),
        stats: Stats {
            total_samples: readings.len(),
            total_incidents: incidents.len(),
            ekf_velocity: snap.es_ekf_state.as_ref().map(|s| s.velocity).unwrap_or(0.0),
            ekf_distance: snap.es_ekf_state.as_ref().map(|s| s.distance).unwrap_or(0.0),
            gps_fixes: gps_count,
        },
        metrics: Metrics {
            test_duration_seconds: uptime,
            accel_samples: accel_count,
            gyro_samples: gyro_count,
            gps_samples: gps_count,
            gravity_magnitude: (snap.gravity_bias.0 * snap.gravity_bias.0
                + snap.gravity_bias.1 * snap.gravity_bias.1
                + snap.gravity_bias.2 * snap.gravity_bias.2)
                .sqrt(),
            gravity_x: snap.gravity_bias.0,
            gravity_y: snap.gravity_bias.1,
            gravity_z: snap.gravity_bias.2,
            gyro_bias_x: snap.gyro_bias.0,
            gyro_bias_y: snap.gyro_bias.1,
            gyro_bias_z: snap.gyro_bias.2,
            calibration_complete,
            gravity_refinements: snap.gravity_refinements,
            gravity_drift_magnitude: snap.gravity_drift,
            gravity_final_x: snap.gravity_bias.0,
            gravity_final_y: snap.gravity_bias.1,
            gravity_final_z: snap.gravity_bias.2,
            peak_memory_mb,
            current_memory_mb,
            covariance_snapshots: covariance_snapshots.clone(),
        },
        system_health: restart_manager.status_report(),
        track_path,
    };

    let filename = save_json_compressed(&output, &args.output_dir, &session_id)?;

    println!(
        "[{}] Final save: {} samples to {}",
        ts_now(),
        readings.len(),
        filename
    );

    if let Some(logger) = session_logger {
        logger.finish()?;
        println!(
            "[{}] Session JSONL closed: {}",
            ts_now(),
            session_json_path
        );
    }

    println!("\n=== Final Stats ===");
    println!("Total accel samples: {}", accel_count);
    println!("Total gyro samples: {}", gyro_count);
    if let Some(ref ekf_state) = snap.es_ekf_state {
        println!("EKF velocity: {:.2} m/s", ekf_state.velocity);
        println!("EKF distance: {:.2} m", ekf_state.distance);
    }

    Ok(())
}

fn ts_now() -> String {
    Utc::now().format("%H:%M:%S").to_string()
}

fn ts_now_clean() -> String {
    Utc::now().format("%Y%m%d_%H%M%S").to_string()
}
