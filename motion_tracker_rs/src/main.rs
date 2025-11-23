#![allow(unused_imports)]
#![allow(unused_mut)]

use anyhow::Result;
use chrono::Utc;
use clap::Parser;
use serde::{Deserialize, Serialize};
use std::collections::VecDeque;
use std::sync::Arc;
use tokio::sync::RwLock;
use std::panic;
use std::fs::{OpenOptions, File};
use std::io::Write;
use std::process::Stdio;
use tokio::sync::mpsc;
use tokio::time::{sleep, Duration};
use tokio::process::Command;
use tokio::io::{AsyncBufReadExt, BufReader};
use nalgebra::Vector3;
use flate2::write::GzEncoder;
use flate2::Compression;

struct LowPassFilter {
    alpha: f64,
    last_output: Vector3<f64>,
    initialized: bool,
}

impl LowPassFilter {
    fn new(cutoff_hz: f64, sample_rate_hz: f64) -> Self {
        let dt = 1.0 / sample_rate_hz;
        let rc = 1.0 / (2.0 * std::f64::consts::PI * cutoff_hz);
        let alpha = dt / (rc + dt);
        Self {
            alpha,
            last_output: Vector3::zeros(),
            initialized: false,
        }
    }

    fn update(&mut self, input: Vector3<f64>) -> Vector3<f64> {
        if !self.initialized {
            self.last_output = input;
            self.initialized = true;
            return input;
        }
        self.last_output = self.last_output * (1.0 - self.alpha) + input * self.alpha;
        self.last_output
    }
}

struct IncidentCooldown {
    last_trigger: f64,
    cooldown_secs: f64,
}

impl IncidentCooldown {
    fn new(cooldown_secs: f64) -> Self {
        Self {
            last_trigger: f64::NEG_INFINITY,
            cooldown_secs,
        }
    }

    fn ready_and_touch(&mut self, now: f64) -> bool {
        if now - self.last_trigger >= self.cooldown_secs {
            self.last_trigger = now;
            return true;
        }
        false
    }
}
mod filters;
mod health_monitor;
mod incident;
mod live_status;
mod restart_manager;
mod types;
mod smoothing;
mod dashboard;
mod physics;
mod rerun_logger;

use filters::complementary::ComplementaryFilter;
use filters::es_ekf::EsEkf;
use filters::ekf_13d::Ekf13d;
use filters::ekf_15d::Ekf15d;
use types::{AccelData, GpsData, GyroData};
use smoothing::AccelSmoother;
use rerun_logger::RerunLogger;

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
    #[arg(long, default_value = "motion_tracker_sessions")]
    output_dir: String,

    /// Dashboard port (default: 8080)
    #[arg(long, default_value = "8080")]
    dashboard_port: u16,
}

#[derive(Serialize, Deserialize, Clone)]
struct SensorReading {
    timestamp: f64,
    accel: Option<AccelData>,
    gyro: Option<GyroData>,
    gps: Option<GpsData>,
    roughness: Option<f64>,
    specific_power_w_per_kg: f64,
    power_coefficient: f64,
    experimental_13d: Option<filters::ekf_13d::Ekf13dState>,
    experimental_15d: Option<filters::ekf_15d::Ekf15dState>,
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

/// Dynamic gravity calibration - continuously refines gravity vector during operation
#[derive(Clone, Debug)]
struct DynamicCalibration {
    /// Accumulated accel samples during current stillness period
    gravity_accumulator: Vec<(f64, f64, f64)>,
    /// Current estimated gravity (updated via EMA)
    gravity_estimate: (f64, f64, f64),
    /// Startup gravity for drift tracking
    gravity_startup: (f64, f64, f64),
    /// Number of calibration refinements applied
    refinement_count: u64,
    /// EMA alpha for smooth gravity updates (0.1 = 10% new, 90% old)
    ema_alpha: f64,
    /// Minimum samples to accumulate before refinement
    min_samples: usize,
    /// Maximum accumulated drift allowed before warning
    drift_threshold: f64,
}

impl DynamicCalibration {
    fn new(initial_gravity: (f64, f64, f64)) -> Self {
        Self {
            gravity_accumulator: Vec::with_capacity(100),
            gravity_estimate: initial_gravity,
            gravity_startup: initial_gravity,
            refinement_count: 0,
            ema_alpha: 0.1,
            min_samples: 30,
            drift_threshold: 0.5, // m/s² drift before warning
        }
    }

    /// Add a sample during stillness period
    fn accumulate(&mut self, ax: f64, ay: f64, az: f64) {
        self.gravity_accumulator.push((ax, ay, az));
    }

    /// Calculate new gravity estimate from accumulated samples
    fn calculate_estimate(&self) -> Option<(f64, f64, f64)> {
        if self.gravity_accumulator.len() < self.min_samples {
            return None;
        }

        let sum: (f64, f64, f64) = self.gravity_accumulator.iter().fold((0.0, 0.0, 0.0), |acc, &(x, y, z)| {
            (acc.0 + x, acc.1 + y, acc.2 + z)
        });

        let count = self.gravity_accumulator.len() as f64;
        Some((sum.0 / count, sum.1 / count, sum.2 / count))
    }

    /// Apply EMA update to gravity estimate
    fn update_with_ema(&mut self, new_gravity: (f64, f64, f64)) {
        self.gravity_estimate = (
            self.ema_alpha * new_gravity.0 + (1.0 - self.ema_alpha) * self.gravity_estimate.0,
            self.ema_alpha * new_gravity.1 + (1.0 - self.ema_alpha) * self.gravity_estimate.1,
            self.ema_alpha * new_gravity.2 + (1.0 - self.ema_alpha) * self.gravity_estimate.2,
        );
        self.refinement_count += 1;
        self.gravity_accumulator.clear();
    }

    /// Get drift since startup
    fn get_drift_magnitude(&self) -> f64 {
        let dx = self.gravity_estimate.0 - self.gravity_startup.0;
        let dy = self.gravity_estimate.1 - self.gravity_startup.1;
        let dz = self.gravity_estimate.2 - self.gravity_startup.2;
        (dx * dx + dy * dy + dz * dz).sqrt()
    }

    /// Check if drift exceeds threshold
    fn drift_warning(&self) -> bool {
        self.get_drift_magnitude() > self.drift_threshold
    }
}

/// Shared sensor state using RwLock for minimal contention
#[derive(Clone)]
struct SensorState {
    pub accel_buffer: Arc<RwLock<VecDeque<AccelData>>>,
    pub gyro_buffer: Arc<RwLock<VecDeque<GyroData>>>,
    pub latest_accel: Arc<RwLock<Option<AccelData>>>,
    pub latest_gyro: Arc<RwLock<Option<GyroData>>>,
    pub latest_gps: Arc<RwLock<Option<GpsData>>>,
    pub accel_count: Arc<RwLock<u64>>,
    pub gyro_count: Arc<RwLock<u64>>,
    pub gps_count: Arc<RwLock<u64>>,
}

impl SensorState {
    fn new() -> Self {
        Self {
            accel_buffer: Arc::new(RwLock::new(VecDeque::with_capacity(1024))),
            gyro_buffer: Arc::new(RwLock::new(VecDeque::with_capacity(1024))),
            latest_accel: Arc::new(RwLock::new(None)),
            latest_gyro: Arc::new(RwLock::new(None)),
            latest_gps: Arc::new(RwLock::new(None)),
            accel_count: Arc::new(RwLock::new(0u64)),
            gyro_count: Arc::new(RwLock::new(0u64)),
            gps_count: Arc::new(RwLock::new(0u64)),
        }
    }
}

/// Calculate gravity and gyro bias from stationary samples
/// Returns (gravity_bias as (x,y,z), gyro_bias as (x,y,z))
fn calculate_biases(
    accel_samples: &std::collections::VecDeque<AccelData>,
    gyro_samples: &std::collections::VecDeque<GyroData>,
) -> ((f64, f64, f64), (f64, f64, f64)) {
    // Calculate mean acceleration (this is the gravity vector when stationary)
    let mut accel_sum = (0.0, 0.0, 0.0);
    let accel_count = accel_samples.len();
    for sample in accel_samples {
        accel_sum.0 += sample.x;
        accel_sum.1 += sample.y;
        accel_sum.2 += sample.z;
    }
    let gravity_bias = if accel_count > 0 {
        (
            accel_sum.0 / accel_count as f64,
            accel_sum.1 / accel_count as f64,
            accel_sum.2 / accel_count as f64,
        )
    } else {
        (0.0, 0.0, 9.81)
    };

    // Calculate mean gyro (zero-rate bias)
    let mut gyro_sum = (0.0, 0.0, 0.0);
    let gyro_count = gyro_samples.len();
    for sample in gyro_samples {
        gyro_sum.0 += sample.x;
        gyro_sum.1 += sample.y;
        gyro_sum.2 += sample.z;
    }
    let gyro_bias = if gyro_count > 0 {
        (
            gyro_sum.0 / gyro_count as f64,
            gyro_sum.1 / gyro_count as f64,
            gyro_sum.2 / gyro_count as f64,
        )
    } else {
        (0.0, 0.0, 0.0)
    };

    (gravity_bias, gyro_bias)
}

/// Combined sensor reader task: Read BOTH accel and gyro from single termux-sensor stream
/// Accel and gyro come from same LSM6DSO IMU, must be in same command
/// Handles multi-line pretty-printed JSON by accumulating until complete object
async fn imu_reader_task(state: SensorState, health_monitor: Arc<HealthMonitor>, enable_gyro: bool) {
    let sensor_list = if enable_gyro {
        "Accelerometer,Gyroscope"
    } else {
        "Accelerometer"
    };
    eprintln!("[imu-reader] Initializing IMU reader (sensors: {})", sensor_list);

    // Cleanup sensor
    let _ = Command::new("termux-sensor").arg("-c").output().await;
    sleep(Duration::from_millis(500)).await;

    // Single termux-sensor command for accel and optionally gyro (no jq - handle JSON in Rust)
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
                            if let Some(values) = sensor_data.get("values").and_then(|v| v.as_array()) {
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
                            if let Some(values) = sensor_data.get("values").and_then(|v| v.as_array()) {
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
                        }
                    }

                    // Log progress every 50 combined updates
                    if (accel_count + gyro_count) % 50 == 0 && (accel_count + gyro_count) > 0 {
                        eprintln!("[imu-reader] Accel: {}, Gyro: {} samples parsed", accel_count, gyro_count);
                    }
                }
            }

            // Clear buffer for next object
            json_buffer.clear();
        }
    }

    eprintln!("[imu-reader] Stream ended: Accel: {}, Gyro: {}", accel_count, gyro_count);
    
    // If stream ends naturally (not crash), we should still signal failure so it restarts
    // But if it was closed explicitly via Abort, this won't be reached.
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
                let dist_sq = (current_point[0] - last[0]).powi(2) +
                              (current_point[1] - last[1]).powi(2);
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

/// Save JSON with gzip compression, returning the actual filename written
fn save_json_compressed(output: &ComparisonOutput, output_dir: &str, is_final: bool) -> Result<String> {
    // Build temp file path
    let temp_path = format!("{}/current_session.json.gz.tmp", output_dir);
    let active_path = format!("{}/current_session.json.gz", output_dir);

    // Serialize to JSON
    let json = serde_json::to_string_pretty(&output)?;

    // Write to temp file with gzip compression
    {
        let file = File::create(&temp_path)?;
        let mut encoder = GzEncoder::new(file, Compression::default());
        encoder.write_all(json.as_bytes())?;
        encoder.finish()?;
    }

    // Atomic rename: move temp -> active
    std::fs::rename(&temp_path, &active_path)?;

    // If final save, rename to timestamped version
    if is_final {
        let final_path = format!("{}/drive_{}.json.gz", output_dir, ts_now_clean());
        std::fs::copy(&active_path, &final_path)?;
        Ok(final_path)
    } else {
        Ok(active_path)
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
                eprintln!("[SUPERVISOR] IMU circuit breaker tripped; exiting to avoid restart loop.");
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
                 eprintln!("[SUPERVISOR] GPS circuit breaker tripped; exiting to avoid restart loop.");
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

    // Initialize filters
    let mut ekf = EsEkf::new(0.05, 8.0, 0.5, args.enable_gyro, 0.0005);
    let mut comp_filter = ComplementaryFilter::new();
    let mut ekf_13d = Ekf13d::new(0.05, 8.0, 0.3, 0.0005); // dt, gps_noise, accel_noise, gyro_noise
    let mut ekf_15d = Ekf15d::new(0.05, 8.0, 0.3, 0.0005); // dt, gps_noise, accel_noise, gyro_noise (with accel bias)
    let mut incident_detector = incident::IncidentDetector::new();
    let mut incidents: Vec<incident::Incident> = Vec::new();
    let mut readings: Vec<SensorReading> = Vec::new();
    let mut trajectories: Vec<TrajectoryPoint> = Vec::new();
    let mut covariance_snapshots: Vec<CovarianceSnapshot> = Vec::new();

    let mut peak_memory_mb: f64 = 0.0;
    let mut current_memory_mb: f64 = 0.0;
    let mut accel_smoother = AccelSmoother::new(9);
    // Low-pass filter for accelerometer to reduce high-frequency jitter (cup holder rattle)
    let mut accel_lpf = LowPassFilter::new(4.0, 50.0);
    let mut incident_cooldown = IncidentCooldown::new(1.0); // 1s cooldown between incident logs
    let mut avg_roughness: f64 = 0.0;

    let start = Utc::now();
    let mut last_save = Utc::now();
    let mut last_status_update = Utc::now();

    // ===== STARTUP CALIBRATION PREAMBLE =====
    println!("[{}] Starting sensor calibration...", ts_now());
    eprintln!("[CALIB] Waiting 3 seconds for sensor data to arrive...");
    sleep(Duration::from_secs(3)).await;

    // Calculate gravity bias and gyro bias from buffer samples with generous retry logic
    let (mut gravity_bias, gyro_bias, calibration_complete) = {
        let accel_buf = sensor_state.accel_buffer.read().await;
        let gyro_buf = sensor_state.gyro_buffer.read().await;
        eprintln!("[CALIB] After 3s: {} accel samples, {} gyro samples", accel_buf.len(), gyro_buf.len());

        if accel_buf.len() < 50 {
            eprintln!("[CALIB] WARNING: Only {} accel samples. Waiting 2 more seconds...", accel_buf.len());
            drop(accel_buf);
            drop(gyro_buf);
            sleep(Duration::from_secs(2)).await;

            let accel_buf = sensor_state.accel_buffer.read().await;
            let gyro_buf = sensor_state.gyro_buffer.read().await;
            eprintln!("[CALIB] After 5s: {} accel samples, {} gyro samples", accel_buf.len(), gyro_buf.len());

            if accel_buf.len() < 50 {
                eprintln!("[CALIB] WARNING: Still only {} samples. Waiting 2 more seconds...", accel_buf.len());
                drop(accel_buf);
                drop(gyro_buf);
                sleep(Duration::from_secs(2)).await;

                let accel_buf = sensor_state.accel_buffer.read().await;
                let gyro_buf = sensor_state.gyro_buffer.read().await;
                eprintln!("[CALIB] After 7s: {} accel samples, {} gyro samples", accel_buf.len(), gyro_buf.len());

                if accel_buf.len() < 50 {
                    eprintln!("[CALIB] FAILED: Still only {} samples after 7 seconds. Using defaults.", accel_buf.len());
                    (
                        (0.0, 0.0, 9.81), // gravity_bias (x, y, z)
                        (0.0, 0.0, 0.0),   // gyro_bias (x, y, z)
                        false,
                    )
                } else {
                    // Calculate biases from available samples
                    let (grav, gyro) = calculate_biases(&accel_buf, &gyro_buf);
                    (grav, gyro, true)
                }
            } else {
                // Calculate biases from available samples
                let (grav, gyro) = calculate_biases(&accel_buf, &gyro_buf);
                (grav, gyro, true)
            }
        } else {
            let (grav, gyro) = calculate_biases(&accel_buf, &gyro_buf);
            (grav, gyro, true)
        }
    };

    eprintln!("[CALIB] Gravity bias vector: ({:.3}, {:.3}, {:.3}) m/s²", gravity_bias.0, gravity_bias.1, gravity_bias.2);
    eprintln!("[CALIB] Gyro bias vector: ({:.6}, {:.6}, {:.6}) rad/s", gyro_bias.0, gyro_bias.1, gyro_bias.2);
    eprintln!("[CALIB] Calibration complete: {}", calibration_complete);

    // Initialize dynamic gravity calibration for runtime refinement
    let mut dyn_calib = DynamicCalibration::new(gravity_bias);
    eprintln!("[CALIB-DYN] Dynamic calibration initialized, will refine gravity during stillness");

    // Duration timeout
    let (duration_tx, mut duration_rx) = mpsc::channel::<()>(1);
    let _duration_handle = if args.duration > 0 {
        let tx = duration_tx.clone();
        let duration_secs = args.duration;
        Some(tokio::spawn(async move {
            sleep(Duration::from_secs(duration_secs)).await;
            eprintln!("[TIMEOUT] Duration timer fired after {} seconds", duration_secs);
            let _ = tx.send(()).await;
        }))
    } else {
        None
    };

    println!("[{}] Starting data collection...", ts_now());

    // Initialize Rerun logger for 3D visualization (v0.15 API compatible)
    let rerun_output_path = format!("motion_tracker_sessions/rerun_{}.rrd", start.format("%Y%m%d_%H%M%S"));
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

    // Virtual kick simulation (keyboard 'k')
    let mut kick_frames_remaining = 0u32;

    // ZUPT (Zero Velocity Update) tracking - detect stillness
    let mut last_accel_mag_raw = 0.0f64;
    let mut last_gyro_mag = 0.0f64;
    let zupt_accel_threshold_low = 9.5;
    let zupt_accel_threshold_high = 10.1;
    let zupt_gyro_threshold = 0.1; // rad/s
    let brake_threshold = 4.0; // m/s^2 (sustained maneuvers)
    let turn_threshold = 4.0;  // m/s^2 (reuse accel magnitude for lateral events)
    let crash_threshold = 20.0; // m/s^2 (instant shocks)

    // GPS tracking
    let mut last_gps_timestamp = 0.0f64;
    let mut is_heading_initialized = false;
    let gps_speed_threshold = 3.0; // m/s - minimum speed to use for heading alignment

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
                    kick_frames_remaining = 10;
                }
            }
        }

        // Acquire read lock on accel buffer
        {
            let gps_snapshot = {
                let g = sensor_state.latest_gps.read().await;
                g.clone()
            };
            let mut buf = sensor_state.accel_buffer.write().await;
            while let Some(accel) = buf.pop_front() {
                // Track filtered acceleration magnitude BEFORE subtracting gravity (for ZUPT detection)
                let raw_vec = Vector3::new(accel.x, accel.y, accel.z);
                let filtered_vec = accel_lpf.update(raw_vec);
                let raw_accel_mag = filtered_vec.norm();
                last_accel_mag_raw = raw_accel_mag;

                // Apply calibration: subtract gravity bias to get true linear acceleration
                let gravity_vec = Vector3::new(gravity_bias.0, gravity_bias.1, gravity_bias.2);
                let corrected_vec = filtered_vec - gravity_vec;
                let mut corrected_x = corrected_vec.x;
                let mut corrected_y = corrected_vec.y;
                let corrected_z = corrected_vec.z;

                // High-pass component (what LPF removed)
                let vibration_vec = raw_vec - filtered_vec;
                let roughness_instant = vibration_vec.norm();
                avg_roughness = avg_roughness * 0.9 + roughness_instant * 0.1;

                // Apply virtual kick if active
                if kick_frames_remaining > 0 {
                    corrected_y += 5.0; // Forward acceleration (Y-axis)
                    kick_frames_remaining -= 1;
                    eprintln!("[KICK] Applied +5.0 m/s² (frames remaining: {})", kick_frames_remaining);
                }

                let corrected_mag = (corrected_x * corrected_x + corrected_y * corrected_y + corrected_z * corrected_z).sqrt();
                let _smoothed_mag = accel_smoother.apply(corrected_mag);

                // Feed accel to 13D filter (prediction phase with zero gyro)
                ekf_13d.predict((corrected_x, corrected_y, corrected_z), (0.0, 0.0, 0.0));

                // Feed raw accel to 15D filter (EKF handles gravity via quaternion)
                // Use filtered_vec (low-pass but NOT gravity-corrected) - 15D subtracts its own bias estimate
                ekf_15d.predict((filtered_vec.x, filtered_vec.y, filtered_vec.z), (0.0, 0.0, 0.0));

                // Activate measurement update for accel bias estimation
                ekf_15d.update_accel((filtered_vec.x, filtered_vec.y, filtered_vec.z));

                // We'll update this reading with 13D/15D data later if gyro arrives
                readings.push(SensorReading {
                    timestamp: accel.timestamp,
                    accel: Some(accel.clone()),
                    gyro: None,
                    gps: None,
                    roughness: Some(avg_roughness),
                    specific_power_w_per_kg: 0.0,
                    power_coefficient: 0.0,
                    experimental_13d: Some(ekf_13d.get_state()),
                    experimental_15d: Some(ekf_15d.get_state()),
                });

                // ===== INCIDENT DETECTION =====
                // Sustained maneuvers (filtered, gravity-removed) gated by EKF speed
                let ekf_speed = ekf.get_state().map(|s| s.velocity).unwrap_or(0.0);
                
                // Extract raw gyro Z (if available) for swerve detection
                let gyro_z = readings.last().and_then(|r| r.gyro.as_ref()).map(|g| g.z).unwrap_or(0.0);
                let (lat, lon, _speed) = gps_snapshot
                    .as_ref()
                    .map(|g| (Some(g.latitude), Some(g.longitude), Some(g.speed)))
                    .unwrap_or((None, None, None));

                // Use unified detector (handles cooldowns internally for swerves, we handle global cooldown here)
                if incident_cooldown.ready_and_touch(accel.timestamp) {
                     // Pass corrected_mag (gravity removed) for maneuvers, but raw shock might need raw_vec
                     // The detector logic we updated expects magnitude in m/s^2
                     // For impacts, we want the raw shock. For maneuvers, we want the linear acceleration.
                     // We'll pass the larger of the two to catch both cases, or prioritize based on magnitude.
                     
                     // Check for impact using RAW vector (includes gravity but shock is huge)
                     let shock_val = raw_vec.norm();
                     
                     // Check for maneuver using CORRECTED vector
                     let maneuver_val = corrected_mag;

                     // Priority to Impact
                     let detection_val = if shock_val > 20.0 { shock_val } else { maneuver_val };
                     
                     if let Some(incident) = incident_detector.detect(
                         detection_val,
                         gyro_z,
                         Some(ekf_speed), // Use EKF speed as it's smoother/more reliable than raw GPS for gating
                         accel.timestamp,
                         lat,
                         lon
                     ) {
                         eprintln!("[INCIDENT] {} Detected: {:.1} (Unit)", incident.incident_type, incident.magnitude);

                         // Log to Rerun 3D visualization
                         if let Some(ref logger) = rerun_logger {
                             if let (Some(lat), Some(lon)) = (incident.latitude, incident.longitude) {
                                 logger.set_time(incident.timestamp);
                                 logger.log_incident(&incident.incident_type, incident.magnitude, lat, lon);
                             }
                         }

                         incidents.push(incident);
                     }
                }

                // Check if stationary (for ZUPT)
                let is_still = raw_accel_mag > zupt_accel_threshold_low && raw_accel_mag < zupt_accel_threshold_high;
                let surface_smooth = avg_roughness < 0.5;

                // ===== DYNAMIC GRAVITY CALIBRATION: Accumulate samples during stillness =====
                if is_still && surface_smooth && ekf_speed < 0.1 {
                    // Accumulate filtered accel reading (before gravity subtraction) for gravity refinement
                    dyn_calib.accumulate(filtered_vec.x, filtered_vec.y, filtered_vec.z);
                }

                // Only update filters if NOT still (to prevent noise integration)
                if !is_still {
                    if args.filter == "ekf" || args.filter == "both" {
                        // Use the new vector-based update (respects sign: + for accel, - for braking)
                        let _ = ekf.update_accelerometer_vector(corrected_x, corrected_y, corrected_z);
                    }
                    if args.filter == "complementary" || args.filter == "both" {
                        let _ = comp_filter.update(corrected_x, corrected_y, corrected_z, 0.0, 0.0, 0.0);
                    }
                }

                // ===== RERUN LOGGING: Log accelerometer data =====
                if let Some(ref logger) = rerun_logger {
                    logger.set_time(accel.timestamp);
                    logger.log_accel_raw(accel.x, accel.y, accel.z);
                    logger.log_accel_filtered(corrected_x, corrected_y, corrected_z);
                }

            }
        }

        // Acquire read lock on gyro buffer
        {
            let mut buf = sensor_state.gyro_buffer.write().await;
            while let Some(gyro) = buf.pop_front() {
                // Apply gyro bias calibration
                let corrected_gx = gyro.x - gyro_bias.0;
                let corrected_gy = gyro.y - gyro_bias.1;
                let corrected_gz = gyro.z - gyro_bias.2;

                // Track gyro magnitude for ZUPT detection
                let gyro_mag = (corrected_gx * corrected_gx + corrected_gy * corrected_gy + corrected_gz * corrected_gz).sqrt();
                last_gyro_mag = gyro_mag;

                if let Some(last) = readings.last_mut() {
                    last.gyro = Some(gyro.clone());

                    // Feed gyro to 13D filter and populate experimental state
                    ekf_13d.predict((0.0, 0.0, 0.0), (corrected_gx, corrected_gy, corrected_gz));
                    last.experimental_13d = Some(ekf_13d.get_state());

                    // Feed gyro to 15D filter (raw gyro with bias subtraction - 15D estimates gyro bias)
                    ekf_15d.predict((0.0, 0.0, 0.0), (corrected_gx, corrected_gy, corrected_gz));

                    // Activate measurement update for gyro bias estimation
                    ekf_15d.update_gyro((corrected_gx, corrected_gy, corrected_gz));
                    last.experimental_15d = Some(ekf_15d.get_state());
                }

                // Only update gyro filter if NOT still
                let is_still = last_accel_mag_raw > zupt_accel_threshold_low && last_accel_mag_raw < zupt_accel_threshold_high;
                if !is_still {
                    if args.filter == "ekf" || args.filter == "both" {
                        let _ = ekf.update_gyroscope(corrected_gx, corrected_gy, corrected_gz);
                    }
                }

                // ===== RERUN LOGGING: Log gyroscope data =====
                if let Some(ref logger) = rerun_logger {
                    logger.set_time(gyro.timestamp);
                    logger.log_gyro_raw(gyro.x, gyro.y, gyro.z);
                }

            }
        }

        // ===== ZUPT CHECK: Force velocity to zero if vehicle is stationary =====
        // Conditions: accel near gravity magnitude AND gyro rotation near zero
        if last_accel_mag_raw > zupt_accel_threshold_low && last_accel_mag_raw < zupt_accel_threshold_high
            && last_gyro_mag < zupt_gyro_threshold
        {
            if args.filter == "ekf" || args.filter == "both" {
                ekf.apply_zupt();
            }
            if args.filter == "complementary" || args.filter == "both" {
                comp_filter.apply_zupt();
            }

            // ===== DYNAMIC GRAVITY REFINEMENT: Apply EMA update if enough samples accumulated =====
            if let Some(new_gravity) = dyn_calib.calculate_estimate() {
                let _old_gravity = dyn_calib.gravity_estimate;
                dyn_calib.update_with_ema(new_gravity);
                let new_estimate = dyn_calib.gravity_estimate;

                // Update the filter's gravity bias for next frame
                gravity_bias = new_estimate;

                eprintln!(
                    "[CALIB-DYN] Refinement #{}: gravity ({:.3}, {:.3}, {:.3}) mag={:.3} drift={:.3}m/s²",
                    dyn_calib.refinement_count,
                    new_estimate.0,
                    new_estimate.1,
                    new_estimate.2,
                    (new_estimate.0 * new_estimate.0 + new_estimate.1 * new_estimate.1 + new_estimate.2 * new_estimate.2).sqrt(),
                    dyn_calib.get_drift_magnitude(),
                );

                // Warn if drift exceeds threshold
                if dyn_calib.drift_warning() {
                    eprintln!(
                        "[CALIB-DYN] WARNING: Gravity drift {:.3}m/s² exceeds threshold {:.3}m/s² - possible sensor degradation",
                        dyn_calib.get_drift_magnitude(),
                        dyn_calib.drift_threshold
                    );
                }
            }
        }

        // ===== GPS INTEGRATION: Check for new GPS fixes and update EKF =====
        {
            let latest_gps = sensor_state.latest_gps.read().await;
            if let Some(gps) = latest_gps.as_ref() {
                if gps.timestamp > last_gps_timestamp {
                    last_gps_timestamp = gps.timestamp;

                    // Update filters with GPS position
                    if args.filter == "ekf" || args.filter == "both" {
                        ekf.update_gps(gps.latitude, gps.longitude, Some(gps.speed), Some(gps.accuracy));
                    }

                    // Update 13D filter with GPS
                    // Use current GPS as origin on first fix; subsequent updates use that origin
                    ekf_13d.update_gps(gps.latitude, gps.longitude, gps.latitude, gps.longitude);

                    // Update 15D filter with GPS (uses lat/lon directly for position correction)
                    ekf_15d.update_gps((gps.latitude, gps.longitude, 0.0));

                    // Motion Alignment: If gps_speed > 3.0 m/s AND heading not yet initialized
                    if gps.speed > gps_speed_threshold && !is_heading_initialized {
                        // Manually set heading from GPS bearing
                        ekf.state_set_heading(gps.bearing.to_radians());
                        is_heading_initialized = true;
                        eprintln!(
                            "[ALIGN] Heading aligned to GPS: {:.1}° (speed: {:.2} m/s)",
                            gps.bearing, gps.speed
                        );
                    }

                    // ===== RECORD GPS TO READINGS FOR DASHBOARD =====
                    // Extract f64 fields directly to avoid cloning GpsData
                    let gps_reading = SensorReading {
                        timestamp: gps.timestamp,
                        accel: None,
                        gyro: None,
                        gps: Some(GpsData {
                            timestamp: gps.timestamp,
                            latitude: gps.latitude,
                            longitude: gps.longitude,
                            accuracy: gps.accuracy,
                            speed: gps.speed,
                            bearing: gps.bearing,
                        }),
                        roughness: None,
                        specific_power_w_per_kg: 0.0,
                        power_coefficient: 0.0,
                        experimental_13d: Some(ekf_13d.get_state()),
                        experimental_15d: Some(ekf_15d.get_state()),
                    };
                    readings.push(gps_reading);
                }
            }
        }

        // Run EKF prediction
        if args.filter == "ekf" || args.filter == "both" {
            let _ = ekf.predict();
        }

        // ===== RERUN LOGGING: Log filter states =====
        if let Some(ref logger) = rerun_logger {
            let elapsed = Utc::now().signed_duration_since(start).num_milliseconds() as f64 / 1000.0;
            logger.set_time(elapsed);

            // Log EKF state (2D filter: position + velocity_vector)
            if let Some(ekf_state) = ekf.get_state() {
                // EsEkf is 2D: velocity_vector is (vx, vy), log with z=0
                logger.log_ekf_velocity(ekf_state.velocity_vector.0, ekf_state.velocity_vector.1, 0.0);
            }

            // Log 13D filter state (orientation + position + velocity)
            let ekf_13d_state = ekf_13d.get_state();
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

            // Log orientation (quaternion) to Rerun for 3D rotation visualization
            logger.log_orientation(
                ekf_13d_state.quaternion.0,
                ekf_13d_state.quaternion.1,
                ekf_13d_state.quaternion.2,
                ekf_13d_state.quaternion.3,
            );

            // Log local XYZ position to Rerun for trajectory visualization
            logger.log_position(
                ekf_13d_state.position.0,
                ekf_13d_state.position.1,
                ekf_13d_state.position.2,
            );

            // Log filter comparison: EKF (8D) vs 13D
            let ekf_speed = ekf.get_state().map(|s| s.velocity).unwrap_or(0.0);
            let ekf_13d_speed = (ekf_13d_state.velocity.0 * ekf_13d_state.velocity.0
                + ekf_13d_state.velocity.1 * ekf_13d_state.velocity.1
                + ekf_13d_state.velocity.2 * ekf_13d_state.velocity.2).sqrt();
            logger.log_filter_comparison("velocity", ekf_speed, ekf_13d_speed);

            // Compare position accuracy
            logger.log_filter_comparison("position_x", 0.0, ekf_13d_state.position.0);
            logger.log_filter_comparison("position_y", 0.0, ekf_13d_state.position.1);
        }

        // Status update every 2 seconds
        let now = Utc::now();
        if (now.signed_duration_since(last_status_update).num_seconds() as i64) >= 2i64 {
            let accel_count = *sensor_state.accel_count.read().await;
            let gyro_count = *sensor_state.gyro_count.read().await;
            let gps_count = *sensor_state.gps_count.read().await;

            let ekf_state = ekf.get_state();
            let comp_state = comp_filter.get_state();
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
            // Circuit breaker time is tracked implicitly in restart state timestamps
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
                    // Note: accel is already corrected (gravity_bias subtracted)
                    let ekf_velocity = ekf_state.as_ref().map(|s| s.velocity).unwrap_or(0.0);
                    let power = physics::calculate_specific_power(
                        accel.x,
                        accel.y,
                        accel.z,
                        ekf_velocity,
                    );
                    live_status.specific_power_w_per_kg = (power.specific_power_w_per_kg * 100.0).round() / 100.0;
                    live_status.power_coefficient = (power.power_coefficient * 100.0).round() / 100.0;
                }
            }
            // Calculate magnitude of calibrated gravity vector
            let gravity_mag = (gravity_bias.0 * gravity_bias.0 + gravity_bias.1 * gravity_bias.1 + gravity_bias.2 * gravity_bias.2).sqrt();
            live_status.gravity_magnitude = gravity_mag;
            live_status.uptime_seconds = uptime;

            if let Some(ekf_state_ref) = ekf_state.as_ref() {
                live_status.ekf_velocity = ekf_state_ref.velocity;
                live_status.ekf_distance = ekf_state_ref.distance;
                live_status.ekf_heading_deg = ekf_state_ref.heading_deg;

                trajectories.push(TrajectoryPoint {
                    timestamp: live_status::current_timestamp(),
                    ekf_x: ekf_state_ref.position_local.0,
                    ekf_y: ekf_state_ref.position_local.1,
                    ekf_velocity: ekf_state_ref.velocity,
                    ekf_heading_deg: ekf_state_ref.heading_deg,
                    comp_velocity: comp_state.as_ref().map(|c| c.velocity).unwrap_or(0.0),
                });

                let (trace, diag) = ekf.get_covariance_snapshot();
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

            if let Some(comp) = comp_state.as_ref() {
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
            let ekf_state = ekf.get_state();
            let elapsed_secs = now.signed_duration_since(start).num_seconds().max(0i64) as u64;
            let gyro_count = *sensor_state.gyro_count.read().await;
            let gps_count = *sensor_state.gps_count.read().await;

            let track_path = build_track_path(&readings);
            let output = ComparisonOutput {
                readings: readings.clone(),
                incidents: incidents.clone(),
                trajectories: trajectories.clone(),
                stats: Stats {
                    total_samples: readings.len(),
                    total_incidents: incidents.len(),
                    ekf_velocity: ekf_state.as_ref().map(|s| s.velocity).unwrap_or(0.0),
                    ekf_distance: ekf_state.as_ref().map(|s| s.distance).unwrap_or(0.0),
                    gps_fixes: gps_count,
                },
                metrics: Metrics {
                    test_duration_seconds: elapsed_secs,
                    accel_samples: accel_count,
                    gyro_samples: gyro_count,
                    gps_samples: gps_count,
                    gravity_magnitude: (gravity_bias.0 * gravity_bias.0 + gravity_bias.1 * gravity_bias.1 + gravity_bias.2 * gravity_bias.2).sqrt(),
                    gravity_x: gravity_bias.0,
                    gravity_y: gravity_bias.1,
                    gravity_z: gravity_bias.2,
                    gyro_bias_x: gyro_bias.0,
                    gyro_bias_y: gyro_bias.1,
                    gyro_bias_z: gyro_bias.2,
                    calibration_complete,
                    gravity_refinements: dyn_calib.refinement_count,
                    gravity_drift_magnitude: dyn_calib.get_drift_magnitude(),
                    gravity_final_x: dyn_calib.gravity_estimate.0,
                    gravity_final_y: dyn_calib.gravity_estimate.1,
                    gravity_final_z: dyn_calib.gravity_estimate.2,
                    peak_memory_mb,
                    current_memory_mb,
                    covariance_snapshots: covariance_snapshots.clone(),
                },
                system_health: restart_manager.status_report(),
                track_path,
            };

            let filename = save_json_compressed(&output, &args.output_dir, false)?;

            println!(
                "[{}] Auto-saved {} samples to {}",
                ts_now(),
                readings.len(),
                filename
            );

            // Don't clear readings - let them accumulate for the dashboard map track
            // For short tests this is fine memory-wise, and GPS data needs to persist
            // Only clear after final save at end of program
            last_save = now;
        }

        // Consumer tick: 20ms (50Hz)
        sleep(Duration::from_millis(20)).await;
    }

    // Final drain of remaining data in buffers BEFORE aborting readers
    // Must process accel first, then pair with gyro (same IMU sensor)
    eprintln!("[CLEANUP] Draining remaining sensor data...");
    loop {
        // Drain accel buffer
        let accel_drained = {
            let mut buf = sensor_state.accel_buffer.write().await;
            let mut count = 0;
            while let Some(accel) = buf.pop_front() {
                let raw_vec = Vector3::new(accel.x, accel.y, accel.z);
                let filtered_vec = accel_lpf.update(raw_vec);
                let gravity_vec = Vector3::new(gravity_bias.0, gravity_bias.1, gravity_bias.2);
                let corrected_vec = filtered_vec - gravity_vec;
                let corrected_mag = (corrected_vec.x * corrected_vec.x + corrected_vec.y * corrected_vec.y + corrected_vec.z * corrected_vec.z).sqrt();
                let _smoothed_mag = accel_smoother.apply(corrected_mag);

                let reading = SensorReading {
                    timestamp: accel.timestamp,
                    accel: Some(accel.clone()),
                    gyro: None,
                    gps: None,
                    roughness: Some(avg_roughness),
                    specific_power_w_per_kg: 0.0,
                    power_coefficient: 0.0,
                    experimental_13d: None,
                    experimental_15d: None,
                };

                readings.push(reading);

                if args.filter == "ekf" || args.filter == "both" {
                    let _ = ekf.update_accelerometer_vector(corrected_vec.x, corrected_vec.y, corrected_vec.z);
                }
                if args.filter == "complementary" || args.filter == "both" {
                    let _ = comp_filter.update(corrected_vec.x, corrected_vec.y, corrected_vec.z, 0.0, 0.0, 0.0);
                }

                // Incident detection on final drain (best-effort)
                if corrected_mag > brake_threshold || corrected_mag > turn_threshold {
                    if incident_cooldown.ready_and_touch(accel.timestamp) {
                        incidents.push(incident::Incident {
                            timestamp: accel.timestamp,
                            incident_type: "hard_maneuver".to_string(),
                            magnitude: corrected_mag,
                            gps_speed: None,
                            latitude: None,
                            longitude: None,
                        });
                    }
                }
                if raw_vec.norm() > crash_threshold {
                    if incident_cooldown.ready_and_touch(accel.timestamp) {
                        incidents.push(incident::Incident {
                            timestamp: accel.timestamp,
                            incident_type: "impact".to_string(),
                            magnitude: raw_vec.norm(),
                            gps_speed: None,
                            latitude: None,
                            longitude: None,
                        });
                    }
                }

                count += 1;
            }
            count
        };

        // Drain gyro buffer and pair with accel readings
        let gyro_drained = {
            let mut buf = sensor_state.gyro_buffer.write().await;
            let mut count = 0;
            while let Some(gyro) = buf.pop_front() {
                if let Some(last) = readings.last_mut() {
                    last.gyro = Some(gyro.clone());
                }

                if args.filter == "ekf" || args.filter == "both" {
                    let _ = ekf.update_gyroscope(gyro.x, gyro.y, gyro.z);
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

    eprintln!("[CLEANUP] Final drain complete: {} readings collected", readings.len());

    // Abort IMU and GPS reader tasks
    println!("[CLEANUP] Aborting IMU reader task...");
    imu_reader_handle.abort();
    println!("[CLEANUP] Aborting GPS reader task...");
    gps_reader_handle.abort();
    tokio::task::yield_now().await;

    // Final stillness clamp: if sensors or GPS say we're stationary, zero velocity/accel
    let latest_gps_speed = sensor_state
        .latest_gps
        .read()
        .await
        .as_ref()
        .map(|g| g.speed)
        .unwrap_or(0.0);
    let stationary_sensors = last_accel_mag_raw > zupt_accel_threshold_low
        && last_accel_mag_raw < zupt_accel_threshold_high
        && last_gyro_mag < zupt_gyro_threshold;
    let stationary = stationary_sensors || latest_gps_speed < 0.3;
    if stationary {
        if args.filter == "ekf" || args.filter == "both" {
            ekf.apply_zupt();
        }
        if args.filter == "complementary" || args.filter == "both" {
            comp_filter.apply_zupt();
        }
    }

    // Final save
    let accel_count = *sensor_state.accel_count.read().await;
    let gyro_count = *sensor_state.gyro_count.read().await;
    let gps_count = *sensor_state.gps_count.read().await;
    let ekf_state = ekf.get_state();
    let uptime = Utc::now().signed_duration_since(start).num_seconds().max(0) as u64;

    let track_path = build_track_path(&readings);
    let output = ComparisonOutput {
        readings: readings.clone(),
        incidents: incidents.clone(),
        trajectories: trajectories.clone(),
        stats: Stats {
            total_samples: readings.len(),
            total_incidents: incidents.len(),
            ekf_velocity: ekf_state.as_ref().map(|s| s.velocity).unwrap_or(0.0),
            ekf_distance: ekf_state.as_ref().map(|s| s.distance).unwrap_or(0.0),
            gps_fixes: gps_count,
        },
        metrics: Metrics {
            test_duration_seconds: uptime,
            accel_samples: accel_count,
            gyro_samples: gyro_count,
            gps_samples: gps_count,
            gravity_magnitude: (gravity_bias.0 * gravity_bias.0 + gravity_bias.1 * gravity_bias.1 + gravity_bias.2 * gravity_bias.2).sqrt(),
            gravity_x: gravity_bias.0,
            gravity_y: gravity_bias.1,
            gravity_z: gravity_bias.2,
            gyro_bias_x: gyro_bias.0,
            gyro_bias_y: gyro_bias.1,
            gyro_bias_z: gyro_bias.2,
            calibration_complete,
            gravity_refinements: dyn_calib.refinement_count,
            gravity_drift_magnitude: dyn_calib.get_drift_magnitude(),
            gravity_final_x: dyn_calib.gravity_estimate.0,
            gravity_final_y: dyn_calib.gravity_estimate.1,
            gravity_final_z: dyn_calib.gravity_estimate.2,
            peak_memory_mb,
            current_memory_mb,
            covariance_snapshots: covariance_snapshots.clone(),
        },
        system_health: restart_manager.status_report(),
        track_path,
    };

    let filename = save_json_compressed(&output, &args.output_dir, true)?;

    println!(
        "[{}] Final save: {} samples to {}",
        ts_now(),
        readings.len(),
        filename
    );

    println!("\n=== Final Stats ===");
    println!("Total accel samples: {}", accel_count);
    println!("Total gyro samples: {}", gyro_count);
    if let Some(ekf_state) = ekf.get_state() {
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
