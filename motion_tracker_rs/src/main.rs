use anyhow::Result;
use chrono::Utc;
use clap::Parser;
use flate2::write::GzEncoder;
use flate2::Compression;
use nalgebra::Vector3;
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

// Runtime tuning constants (promoted from replay experiments)
const GPS_VEL_STD: f64 = 0.3;
const NORMAL_CLAMP_SCALE: f64 = 1.5;
const NORMAL_CLAMP_OFFSET: f64 = 5.0;
const GAP_CLAMP_SCALE: f64 = 1.1;
const GAP_CLAMP_OFFSET: f64 = 2.0;
const GAP_CLAMP_TRIGGER: f64 = 5.0;
const GAP_CLAMP_HYST: f64 = 0.5;

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

// 2nd-order high-pass filter (Butterworth 3 Hz @ 50 Hz sample rate) for road roughness
struct HighPassFilter {
    x1: f64,
    x2: f64,
    y1: f64,
    y2: f64,
}

impl HighPassFilter {
    fn new() -> Self {
        Self {
            x1: 0.0,
            x2: 0.0,
            y1: 0.0,
            y2: 0.0,
        }
    }

    fn filter(&mut self, x: f64) -> f64 {
        // Coefficients from scipy.signal.butter(2, 3, 'high', fs=50)
        const B: [f64; 3] = [0.8371, -1.6742, 0.8371];
        const A: [f64; 3] = [1.0, -1.6475, 0.7009];

        let y = B[0] * x + B[1] * self.x1 + B[2] * self.x2 - A[1] * self.y1 - A[2] * self.y2;

        self.x2 = self.x1;
        self.x1 = x;
        self.y2 = self.y1;
        self.y1 = y;

        y
    }
}

struct RoughnessEstimator {
    hp_x: HighPassFilter,
    hp_y: HighPassFilter,
    hp_z: HighPassFilter,
    window: VecDeque<f64>,
    window_size: usize,
    ewma: f64,
    alpha: f64,
}

impl RoughnessEstimator {
    fn new(window_size: usize, alpha: f64) -> Self {
        Self {
            hp_x: HighPassFilter::new(),
            hp_y: HighPassFilter::new(),
            hp_z: HighPassFilter::new(),
            window: VecDeque::with_capacity(window_size),
            window_size,
            ewma: 0.0,
            alpha,
        }
    }

    fn update(&mut self, ax: f64, ay: f64, az: f64) -> f64 {
        // High-pass each axis to isolate vibration content
        let hx = self.hp_x.filter(ax);
        let hy = self.hp_y.filter(ay);
        let hz = self.hp_z.filter(az);

        // Accumulate squared magnitude into a sliding window
        let vib_sq = hx * hx + hy * hy + hz * hz;
        self.window.push_back(vib_sq);
        if self.window.len() > self.window_size {
            self.window.pop_front();
        }

        // RMS of the high-passed magnitude
        let rms = (self.window.iter().sum::<f64>() / self.window.len().max(1) as f64).sqrt();

        // Smooth for stability
        self.ewma = self.alpha * rms + (1.0 - self.alpha) * self.ewma;
        self.ewma
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
mod dashboard;
mod filters;
mod health_monitor;
mod incident;
mod live_status;
mod physics;
mod rerun_logger;
mod restart_manager;
mod smoothing;
mod types;

use filters::complementary::ComplementaryFilter;
use filters::ekf_15d::Ekf15d;
use filters::es_ekf::EsEkf;
use rerun_logger::RerunLogger;
use smoothing::AccelSmoother;
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
    experimental_15d: Option<filters::ekf_15d::Ekf15dState>,
    fgo: Option<filters::fgo::FgoState>, // Factor Graph Optimization (shadow mode)
}

#[derive(Serialize, Deserialize, Clone)]
struct TrajectoryPoint {
    timestamp: f64,
    ekf_x: f64,
    ekf_y: f64,
    ekf_velocity: f64,
    ekf_heading_deg: f64,
    comp_velocity: f64,
    #[serde(default)]
    lat: Option<f64>,
    #[serde(default)]
    lon: Option<f64>,
    /// True once the ES-EKF origin has been initialized (prevents plotting 0,0 points)
    #[serde(default)]
    valid: bool,
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
    #[serde(default)]
    origin_lat: Option<f64>,
    #[serde(default)]
    origin_lon: Option<f64>,
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

        let sum: (f64, f64, f64) = self
            .gravity_accumulator
            .iter()
            .fold((0.0, 0.0, 0.0), |acc, &(x, y, z)| {
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

    // Initialize filters
    let mut ekf = EsEkf::new(0.05, 8.0, 0.5, args.enable_gyro, 0.0005);
    let mut comp_filter = ComplementaryFilter::new();
    let mut ekf_15d = Ekf15d::new(0.05, 8.0, 0.3, 0.0005); // dt, gps_noise, accel_noise, gyro_noise (with accel bias)
    let mut incident_detector = incident::IncidentDetector::new();
    let mut incidents: Vec<incident::Incident> = Vec::new();
    let mut readings: Vec<SensorReading> = Vec::new();
    let mut trajectories: Vec<TrajectoryPoint> = Vec::new();
    let mut covariance_snapshots: Vec<CovarianceSnapshot> = Vec::new();
    let mut origin_latlon: Option<(f64, f64)> = None;

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
                    (
                        (0.0, 0.0, 9.81), // gravity_bias (x, y, z)
                        (0.0, 0.0, 0.0),  // gyro_bias (x, y, z)
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

    eprintln!(
        "[CALIB] Gravity bias vector: ({:.3}, {:.3}, {:.3}) m/s²",
        gravity_bias.0, gravity_bias.1, gravity_bias.2
    );
    eprintln!(
        "[CALIB] Gyro bias vector: ({:.6}, {:.6}, {:.6}) rad/s",
        gyro_bias.0, gyro_bias.1, gyro_bias.2
    );
    eprintln!("[CALIB] Calibration complete: {}", calibration_complete);

    // Initialize dynamic gravity calibration for runtime refinement
    let mut dyn_calib = DynamicCalibration::new(gravity_bias);
    eprintln!("[CALIB-DYN] Dynamic calibration initialized, will refine gravity during stillness");

    // Initialize Factor Graph Optimization (FGO) in shadow mode
    let start_pos = (0.0, 0.0, 0.0); // Will be updated by first GPS fix
    let start_vel = (0.0, 0.0, 0.0);
    let start_bias = (0.0, 0.0, 0.0); // FGO handles gravity internally, not as bias
    let mut fgo = filters::fgo::GraphEstimator::new(start_pos, start_vel, start_bias);
    eprintln!("[FGO] Factor Graph Optimizer initialized (shadow mode)");

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

    // Virtual kick simulation (keyboard 'k')
    let mut kick_frames_remaining = 0u32;

    // ZUPT (Zero Velocity Update) tracking - detect stillness
    let mut last_accel_mag_raw = 0.0f64;
    let mut last_gyro_mag = 0.0f64;
    let zupt_accel_threshold_low = 9.5;
    let zupt_accel_threshold_high = 10.1;
    let zupt_gyro_threshold = 0.1; // rad/s
    let brake_threshold = 4.0; // m/s^2 (sustained maneuvers)
    let turn_threshold = 4.0; // m/s^2 (reuse accel magnitude for lateral events)
    let crash_threshold = 20.0; // m/s^2 (instant shocks)

    // GPS tracking
    let mut last_gps_timestamp = 0.0f64;
    let mut is_heading_initialized = false;
    let mut last_accel_ts: Option<f64> = None;
    let mut last_gyro_ts: Option<f64> = None;
    let mut recent_gps_speeds: VecDeque<(f64, f64)> = VecDeque::new(); // (timestamp, speed)
    let gps_speed_window = 10.0;
    let mut last_speed_clamp_ts: f64 = -1.0;
    let mut last_nhc_ts: f64 = -1.0;
    let mut last_gps_speed: f64 = 0.0;
    let mut in_gap_mode: bool = false;
    let mut last_gps_fix_ts: Option<f64> = None;
    let mut last_baro: Option<types::BaroData> = None;
    let mut roughness_estimator = RoughnessEstimator::new(50, 0.1); // 1s RMS window @50Hz, light EWMA

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
                if let Some(prev_ts) = last_accel_ts {
                    let dt = accel.timestamp - prev_ts;
                    if dt <= 0.0 || dt > 1.0 {
                        last_accel_ts = Some(accel.timestamp);
                        continue;
                    }
                }
                last_accel_ts = Some(accel.timestamp);

                // Track filtered acceleration magnitude BEFORE subtracting gravity (for ZUPT detection)
                let raw_vec = Vector3::new(accel.x, accel.y, accel.z);
                let filtered_vec = accel_lpf.update(raw_vec);
                let raw_accel_mag = filtered_vec.norm();
                last_accel_mag_raw = raw_accel_mag;

                // Apply calibration: subtract gravity bias to get true linear acceleration
                let gravity_vec = Vector3::new(gravity_bias.0, gravity_bias.1, gravity_bias.2);
                let corrected_vec = filtered_vec - gravity_vec;
                let corrected_x = corrected_vec.x;
                let mut corrected_y = corrected_vec.y;
                let corrected_z = corrected_vec.z;

                // Road roughness: high-pass + RMS to isolate vibration
                let roughness_value = roughness_estimator.update(
                    corrected_vec.x,
                    corrected_vec.y,
                    corrected_vec.z,
                );
                avg_roughness = roughness_value;

                // Apply virtual kick if active
                if kick_frames_remaining > 0 {
                    corrected_y += 5.0; // Forward acceleration (Y-axis)
                    kick_frames_remaining -= 1;
                    eprintln!(
                        "[KICK] Applied +5.0 m/s² (frames remaining: {})",
                        kick_frames_remaining
                    );
                }

                let corrected_mag = (corrected_x * corrected_x
                    + corrected_y * corrected_y
                    + corrected_z * corrected_z)
                    .sqrt();
                let _smoothed_mag = accel_smoother.apply(corrected_mag);

                // Simple specific power estimate using scalar accel and available speed (GPS preferred)
                let speed_for_power = gps_snapshot
                    .as_ref()
                    .map(|g| g.speed)
                    .or_else(|| comp_filter.get_state().map(|c| c.velocity))
                    .or_else(|| ekf.get_state().map(|s| s.velocity))
                    .unwrap_or(0.0);
                let accel_mag = (accel.x * accel.x + accel.y * accel.y + accel.z * accel.z).sqrt();
                let forward_accel_approx = (accel_mag - 9.81).abs();
                let specific_power_est = if speed_for_power > 1.0 {
                    forward_accel_approx * speed_for_power
                } else {
                    0.0
                };

                // Velocity sanity gate: prevent EKF from exceeding recent GPS speed envelope.
                let max_recent_gps = recent_gps_speeds
                    .iter()
                    .map(|(_, s)| *s)
                    .fold(0.0_f64, f64::max);
                if max_recent_gps > 3.0 {
                    let ekf_speed = ekf_15d.get_speed();
                    let now_ts = accel.timestamp;
                    let gap_for_clamp = last_gps_fix_ts.map(|ts| (now_ts - ts).max(0.0)).unwrap_or(f64::INFINITY);
                    // Envelope clamp remains, but gap-mode per-prediction clamp above should catch outages
                    let (scale, offset, min_interval) = if gap_for_clamp > 5.0 {
                        (GAP_CLAMP_SCALE, GAP_CLAMP_OFFSET, 0.0)
                    } else {
                        (NORMAL_CLAMP_SCALE, NORMAL_CLAMP_OFFSET, 0.0)
                    };
                    let limit = scale * max_recent_gps + offset;
                    if ekf_speed > limit && ekf_speed > 1e-3 && (now_ts - last_speed_clamp_ts) > min_interval {
                        eprintln!(
                            "[CLAMP] t={:.1}s gap={:.1}s speed {:.1} -> limit {:.1}",
                            now_ts, gap_for_clamp, ekf_speed, limit
                        );
                        ekf_15d.clamp_speed(limit);
                        last_speed_clamp_ts = now_ts;
                    }
                }

                // Feed raw accel to 15D filter (EKF handles gravity via quaternion)
                // Use filtered_vec (low-pass but NOT gravity-corrected) - 15D subtracts its own bias estimate
                ekf_15d.predict(
                    (filtered_vec.x, filtered_vec.y, filtered_vec.z),
                    (0.0, 0.0, 0.0),
                );

                // Magnetometer yaw assist during GPS gaps (>3s)
                let gps_gap = if let Some(ts) = last_gps_fix_ts {
                    (accel.timestamp - ts).max(0.0)
                } else {
                    f64::INFINITY
                };
                // Gap-mode speed ceiling during GPS outages (per prediction clamp)
                if let Some(ts) = last_gps_fix_ts {
                    let gap = (accel.timestamp - ts).max(0.0);
                    // Enter gap mode after threshold; hysteresis keeps us in gap mode until GPS returns
                    if gap > GAP_CLAMP_TRIGGER || (in_gap_mode && gap > GAP_CLAMP_HYST) {
                        in_gap_mode = true;
                    }
                    if in_gap_mode {
                        let limit = if last_gps_speed < 1.0 {
                            2.0 // stationary: very tight cap
                        } else if last_gps_speed < 5.0 {
                            last_gps_speed * 2.0 + GAP_CLAMP_OFFSET // low speed: moderate headroom
                        } else {
                            GAP_CLAMP_SCALE * last_gps_speed + GAP_CLAMP_OFFSET // tighter headroom during outages
                        }
                        .max(2.0); // floor
                        let ekf_speed = ekf_15d.get_speed();
                        if ekf_speed > limit {
                            eprintln!(
                                "[GAP CLAMP] t={:.1}s gap={:.1}s speed {:.1} -> limit {:.1}",
                                accel.timestamp, gap, ekf_speed, limit
                            );
                            ekf_15d.clamp_speed(limit);
                        }
                    }
                } else {
                    // No GPS yet; avoid gap mode until first fix
                    in_gap_mode = false;
                }

                // Consider GPS gap once per accel tick
                let in_gps_gap = gps_gap > 3.0;

                // Barometer: adjust vertical velocity prior based on pressure stability during GPS gaps
                // Only apply when moving; at rest this can inject noise
                if args.enable_baro && in_gps_gap {
                    if let Some(baro) = sensor_state.latest_baro.read().await.as_ref().cloned() {
                        let is_new = last_baro
                            .as_ref()
                            .map(|b| (b.timestamp - baro.timestamp).abs() > 1e-6)
                            .unwrap_or(true);
                        if is_new {
                            if let Some(prev) = last_baro.as_ref() {
                                let dt = (baro.timestamp - prev.timestamp).max(1e-3);
                                let dp_dt_hpa = (baro.pressure_hpa - prev.pressure_hpa) / dt;
                                let dp_dt_pa = dp_dt_hpa * 100.0;
                                let pressure_stable = dp_dt_pa.abs() < 0.5; // ~0.4 m/s vertical
                                // Gate by speed: only constrain while moving (use last GPS speed)
                                if last_gps_speed > 1.0 {
                                    let noise_var = if pressure_stable { 5e-3 } else { 1e-1 }; // gentle damping when stable
                                    ekf_15d.zero_vertical_velocity(noise_var);
                                }
                            }
                            last_baro = Some(baro);
                        }
                    }
                }

                // Non-holonomic constraint: clamp lateral/vertical body velocity at reduced rate.
                // Disable after very long GPS gaps to avoid constraining with stale heading.
                if last_nhc_ts < 0.0 || (accel.timestamp - last_nhc_ts) >= 1.0 {
                    let nhc_gap = last_gps_fix_ts
                        .map(|ts| (accel.timestamp - ts).max(0.0))
                        .unwrap_or(0.0);
                    // Soften with gap; after 10s gap, skip NHC entirely.
                    if nhc_gap <= 10.0 {
                        let nhc_r = (1.0 + nhc_gap * 0.5).min(5.0);
                        ekf_15d.update_body_velocity(Vector3::zeros(), nhc_r);
                    } else {
                        eprintln!("[NHC SKIP] gap {:.1}s", nhc_gap);
                    }
                    last_nhc_ts = accel.timestamp;
                }

                if args.enable_mag && in_gps_gap {
                    // Only trust mag heading when moving; otherwise heading noise can rotate the body frame at rest
                    if last_gps_speed > 2.0 && ekf_15d.get_speed() > 2.0 {
                        if let Some(mag) = sensor_state.latest_mag.read().await.as_ref() {
                            if let Some(innov) = ekf_15d.update_mag_heading(
                                mag,
                                0.157, // approx 9° declination in radians (Tucson)
                            ) {
                                eprintln!(
                                    "[MAG] gap {:.1}s yaw correction: {:.1}°",
                                    gps_gap,
                                    innov.to_degrees()
                                );
                            }
                        }
                    }
                }

                // Feed FGO preintegrator (fast loop - non-blocking)
                // Use corrected acceleration (gravity removed) and zero gyro for now
                let accel_vec_fgo = Vector3::new(corrected_x, corrected_y, corrected_z);
                let gyro_vec_fgo = Vector3::zeros(); // Will be updated when gyro arrives
                fgo.enqueue_imu(accel_vec_fgo, gyro_vec_fgo, accel.timestamp);

                // We'll update this reading with 13D/15D data later if gyro arrives
                let reading = SensorReading {
                    timestamp: accel.timestamp,
                    accel: Some(accel.clone()),
                    gyro: None,
                    gps: None,
                    baro: sensor_state.latest_baro.read().await.clone(),
                    roughness: Some(avg_roughness),
                    specific_power_w_per_kg: specific_power_est,
                    power_coefficient: 0.0,
                    experimental_15d: Some(ekf_15d.get_state()),
                    mag: sensor_state.latest_mag.read().await.clone(),
                    fgo: None, // Will be updated on GPS fix
                };

                log_jsonl_reading(&mut session_logger, &reading, &mut jsonl_count)?;
                readings.push(reading);

                // ===== INCIDENT DETECTION =====
                // Sustained maneuvers (filtered, gravity-removed) gated by EKF speed
                let ekf_speed = ekf.get_state().map(|s| s.velocity).unwrap_or(0.0);

                // Extract raw gyro Z (if available) for swerve detection
                let gyro_z = readings
                    .last()
                    .and_then(|r| r.gyro.as_ref())
                    .map(|g| g.z)
                    .unwrap_or(0.0);
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
                    let detection_val = if shock_val > 20.0 {
                        shock_val
                    } else {
                        maneuver_val
                    };

                    if let Some(incident) = incident_detector.detect(
                        detection_val,
                        gyro_z,
                        None, // Decoupled from filter speed; rely on raw dynamics
                        accel.timestamp,
                        lat,
                        lon,
                    ) {
                        eprintln!(
                            "[INCIDENT] {} Detected: {:.1} (Unit)",
                            incident.incident_type, incident.magnitude
                        );

                        // Log to Rerun 3D visualization
                        if let Some(ref logger) = rerun_logger {
                            if let (Some(lat), Some(lon)) = (incident.latitude, incident.longitude)
                            {
                                logger.set_time(incident.timestamp);
                                logger.log_incident(
                                    &incident.incident_type,
                                    incident.magnitude,
                                    lat,
                                    lon,
                                );
                            }
                        }

                        incidents.push(incident);
                    }
                }

                // Check if stationary (for ZUPT)
                let is_still = raw_accel_mag > zupt_accel_threshold_low
                    && raw_accel_mag < zupt_accel_threshold_high;
                let surface_smooth = avg_roughness < 0.5;

                // ===== DYNAMIC GRAVITY CALIBRATION: Accumulate samples during stillness =====
                if is_still && surface_smooth && ekf_speed < 0.1 {
                    // Accumulate filtered accel reading (before gravity subtraction) for gravity refinement
                    dyn_calib.accumulate(filtered_vec.x, filtered_vec.y, filtered_vec.z);

                    // 15D Bias/Attitude Update (Gravity Alignment)
                    ekf_15d.update_stationary_accel((
                        filtered_vec.x,
                        filtered_vec.y,
                        filtered_vec.z,
                    ));
                }

                // Only update filters if NOT still (to prevent noise integration)
                if !is_still {
                    if args.filter == "ekf" || args.filter == "both" {
                        // Use the new vector-based update (respects sign: + for accel, - for braking)
                        let _ =
                            ekf.update_accelerometer_vector(corrected_x, corrected_y, corrected_z);
                    }
                    if args.filter == "complementary" || args.filter == "both" {
                        let _ = comp_filter.update(
                            corrected_x,
                            corrected_y,
                            corrected_z,
                            0.0,
                            0.0,
                            0.0,
                        );
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
                if let Some(prev_ts) = last_gyro_ts {
                    let dt = gyro.timestamp - prev_ts;
                    if dt <= 0.0 || dt > 1.0 {
                        last_gyro_ts = Some(gyro.timestamp);
                        continue;
                    }
                }
                last_gyro_ts = Some(gyro.timestamp);

                // Apply gyro bias calibration
                let corrected_gx = gyro.x - gyro_bias.0;
                let corrected_gy = gyro.y - gyro_bias.1;
                let mut corrected_gz = gyro.z - gyro_bias.2;

                // Straight-road clamp: freeze yaw drift when wheel is effectively straight
                let ekf15_speed = ekf_15d.get_speed();
                if corrected_gz.abs() < 0.02 && ekf15_speed > 5.0 {
                    corrected_gz = 0.0;
                }

                // Track gyro magnitude for ZUPT detection
                let gyro_mag = (corrected_gx * corrected_gx
                    + corrected_gy * corrected_gy
                    + corrected_gz * corrected_gz)
                    .sqrt();
                last_gyro_mag = gyro_mag;

                if let Some(last) = readings.last_mut() {
                    last.gyro = Some(gyro.clone());

                    // Feed gyro to 15D filter (raw gyro with bias subtraction - 15D estimates gyro bias)
                    ekf_15d.predict((0.0, 0.0, 0.0), (corrected_gx, corrected_gy, corrected_gz));

                    // Conditional Bias Update (ZUPT)
                    if last_accel_mag_raw > 9.5 && last_accel_mag_raw < 10.1 && gyro_mag < 0.1 {
                        // Use raw gyro for bias estimation
                        ekf_15d.update_stationary_gyro((gyro.x, gyro.y, gyro.z));
                    }

                    last.experimental_15d = Some(ekf_15d.get_state());

                    // Feed FGO preintegrator with gyro data (fast loop)
                    let gyro_vec_fgo = Vector3::new(corrected_gx, corrected_gy, corrected_gz);
                    // Use the accel from this reading (already stored)
                    if let Some(ref accel_data) = last.accel {
                        let accel_vec_fgo = Vector3::new(
                            accel_data.x - gravity_bias.0,
                            accel_data.y - gravity_bias.1,
                            accel_data.z - gravity_bias.2,
                        );
                        fgo.enqueue_imu(accel_vec_fgo, gyro_vec_fgo, gyro.timestamp);
                    }
                }

                // Only update gyro filter if NOT still
                let is_still = last_accel_mag_raw > zupt_accel_threshold_low
                    && last_accel_mag_raw < zupt_accel_threshold_high;
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
        if last_accel_mag_raw > zupt_accel_threshold_low
            && last_accel_mag_raw < zupt_accel_threshold_high
            && last_gyro_mag < zupt_gyro_threshold
        {
            if args.filter == "ekf" || args.filter == "both" {
                ekf.apply_zupt();
            }
            if args.filter == "complementary" || args.filter == "both" {
                comp_filter.apply_zupt();
            }
            // Apply ZUPT to experimental 15D as well
            if let Some(last) = readings.last() {
                if let Some(accel_last) = last.accel.as_ref() {
                    let accel_vec =
                        nalgebra::Vector3::new(accel_last.x, accel_last.y, accel_last.z);
                    ekf_15d.apply_zupt(&accel_vec);
                }
            }
            // Clamp experimental 15D state while stationary
            ekf_15d.force_zero_velocity();

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
                    // Basic GPS gating: relaxed accuracy, no implied-accel rejection
                    if gps.accuracy > 50.0 {
                        eprintln!(
                            "[GPS] Rejected fix (acc={:.1}m, speed={:.2}m/s) as outlier",
                            gps.accuracy, gps.speed
                        );
                        continue;
                    }

                    last_gps_timestamp = gps.timestamp;
                    // Compute measurement latency and project position forward
                    let system_now = std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .unwrap_or_default()
                        .as_secs_f64();
                    let latency = (system_now - gps.timestamp).max(0.0);
                    if latency > 1.0 {
                        eprintln!("[GPS] High latency: {:.2}s", latency);
                    }

                    // Rough forward projection using current 15D velocity (ENU), guarded
                    let ekf_state = ekf_15d.get_state();
                    let vx = ekf_state.velocity.0;
                    let vy = ekf_state.velocity.1;
                    let speed = (vx * vx + vy * vy).sqrt();
                let (gps_proj_lat, gps_proj_lon) = if latency < 1.0 && speed < 50.0 {
                    let lat_proj = gps.latitude
                        + (vy * latency) / 6371000.0 * 180.0 / std::f64::consts::PI;
                    let lon_proj = gps.longitude
                        + (vx * latency)
                                / (6371000.0 * (gps.latitude.to_radians().cos() + 1e-9))
                                * 180.0
                                / std::f64::consts::PI;
                        (lat_proj, lon_proj)
                    } else {
                        if speed >= 50.0 {
                            eprintln!(
                                "[GPS] Skipping projection: high speed {:.1} m/s (latency {:.2}s)",
                                speed, latency
                            );
                        }
                        (gps.latitude, gps.longitude)
                    };

                    // Update filters with GPS position
                    if args.filter == "ekf" || args.filter == "both" {
                        ekf.update_gps(
                            gps_proj_lat,
                            gps_proj_lon,
                            Some(gps.speed),
                            Some(gps.accuracy),
                        );
                    }

                    // Track recent GPS speeds for sanity gating
                    recent_gps_speeds.push_back((gps.timestamp, gps.speed));
                    while let Some((ts, _)) = recent_gps_speeds.front() {
                        if gps.timestamp - *ts > gps_speed_window {
                            recent_gps_speeds.pop_front();
                        } else {
                            break;
                        }
                    }
                    last_gps_fix_ts = Some(gps.timestamp);
                    last_gps_speed = gps.speed;

                    // COLD START PROTOCOL: Initialize on first GPS fix, update on subsequent
                    let is_first_gps_fix = origin_latlon.is_none();

                    if is_first_gps_fix {
                        // FIRST FIX: Initialize origin and EKF state, DO NOT update
                        // This prevents "Null Island" teleport (0,0,0) → (lat,lon) causing massive innovation
                        ekf_15d.set_origin(gps.latitude, gps.longitude, 0.0);
                        origin_latlon = Some((gps.latitude, gps.longitude));

                        // Initialize velocity from GPS (prevents 0 → speed causing acceleration spike)
                        ekf_15d.force_zero_velocity(); // Start from rest

                        println!("[COLD START] GPS Locked. Origin: ({:.6}, {:.6}). EKF initialized at REST.",
                                 gps.latitude, gps.longitude);
                        println!("[COLD START] Skipping first GPS update to prevent initialization shock.");
                    } else {
                        // SUBSEQUENT FIXES: Normal updates
                        // Update 15D filter with GPS (uses lat/lon directly for position correction)
                        ekf_15d.update_gps((gps_proj_lat, gps_proj_lon, 0.0), gps.accuracy);
                        // Update 15D velocity using GPS speed/bearing when available (fixed R)
                        ekf_15d.update_gps_velocity(gps.speed, gps.bearing.to_radians(), GPS_VEL_STD);
                    }

                    // If GPS indicates near-zero speed, clamp 15D velocity to zero
                    if gps.speed < 0.5 {
                        ekf_15d.update_velocity((0.0, 0.0, 0.0), 1e-3);
                    } else {
                        // GPS Velocity Update (fixed R)
                        ekf_15d.update_gps_velocity(gps.speed, gps.bearing.to_radians(), GPS_VEL_STD);
                        // Land vehicle assumption: clamp vertical velocity tightly
                        ekf_15d.zero_vertical_velocity(1e-4);
                    }

                    // Trigger FGO optimization (slow loop - GPS fixes ~1Hz)
                    fgo.add_gps_measurement(
                        gps.latitude,
                        gps.longitude,
                        0.0,
                        gps.timestamp,
                        gps.speed,
                    );
                    let fgo_stats = fgo.get_stats();
                    if fgo_stats.2 % 10 == 0 && fgo_stats.2 > 0 {
                        // Log every 10 optimizations
                        eprintln!(
                            "[FGO] Optimization #{}: {} nodes, {} GPS factors",
                            fgo_stats.2, fgo_stats.0, fgo_stats.1
                        );
                    }

                    // Motion Alignment: If gps_speed > 5.0 m/s AND heading not yet initialized
                    if gps.speed > 5.0 && !is_heading_initialized {
                        // Align heading to GPS bearing.
                        // GPS bearing: degrees, clockwise from North.
                        // EKF yaw (ENU CCW from East): yaw = 90° - bearing
                        let gps_yaw = (90.0 - gps.bearing).to_radians();
                        ekf.state_set_heading(gps_yaw);
                        // Set 15D quaternion to the yaw-only rotation
                        let half = gps_yaw * 0.5;
                        ekf_15d.state[6] = half.cos(); // w
                        ekf_15d.state[7] = 0.0;        // x
                        ekf_15d.state[8] = 0.0;        // y
                        ekf_15d.state[9] = half.sin(); // z
                        is_heading_initialized = true;
                        eprintln!(
                            "[ALIGN] Heading aligned to GPS: bearing {:.1}° -> yaw {:.1}° (speed: {:.2} m/s)",
                            gps.bearing,
                            gps_yaw.to_degrees(),
                            gps.speed
                        );
                    }

                    // ===== RECORD GPS TO READINGS FOR DASHBOARD =====
                    // Extract f64 fields directly to avoid cloning GpsData
                    let gps_reading = SensorReading {
                        timestamp: gps.timestamp,
                        accel: None,
                        gyro: None,
                        mag: sensor_state.latest_mag.read().await.clone(),
                        baro: sensor_state.latest_baro.read().await.clone(),
                        gps: Some(GpsData {
                            timestamp: gps.timestamp,
                            latitude: gps_proj_lat,
                            longitude: gps_proj_lon,
                            accuracy: gps.accuracy,
                            speed: gps.speed,
                            bearing: gps.bearing,
                        }),
                        roughness: None,
                        specific_power_w_per_kg: 0.0,
                        power_coefficient: 0.0,
                        experimental_15d: Some(ekf_15d.get_state()),
                        fgo: Some(fgo.get_current_state()), // FGO shadow mode output
                    };
                    log_jsonl_reading(&mut session_logger, &gps_reading, &mut jsonl_count)?;
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
            let elapsed =
                Utc::now().signed_duration_since(start).num_milliseconds() as f64 / 1000.0;
            logger.set_time(elapsed);

            // Log EKF state (2D filter: position + velocity_vector)
            if let Some(ekf_state) = ekf.get_state() {
                // EsEkf is 2D: velocity_vector is (vx, vy), log with z=0
                logger.log_ekf_velocity(
                    ekf_state.velocity_vector.0,
                    ekf_state.velocity_vector.1,
                    0.0,
                );
            }

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

                // Calculate virtual dyno specific power (scalar: |a|-g times speed)
                if let Some(accel) = sensor_state.latest_accel.read().await.as_ref() {
                    let calc_velocity = if gps.speed > 0.1 {
                        gps.speed
                    } else {
                        comp_state.as_ref().map(|c| c.velocity).unwrap_or(0.0)
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
            let gravity_mag = (gravity_bias.0 * gravity_bias.0
                + gravity_bias.1 * gravity_bias.1
                + gravity_bias.2 * gravity_bias.2)
                .sqrt();
            live_status.gravity_magnitude = gravity_mag;
            live_status.uptime_seconds = uptime;

                if let Some(ekf_state_ref) = ekf_state.as_ref() {
                    live_status.ekf_velocity = ekf_state_ref.velocity;
                    live_status.ekf_distance = ekf_state_ref.distance;
                    live_status.ekf_heading_deg = ekf_state_ref.heading_deg;
                    let origin_ready = ekf_state_ref.gps_updates > 0;
                    let (lat_opt, lon_opt) = if origin_ready {
                        (Some(ekf_state_ref.position.0), Some(ekf_state_ref.position.1))
                    } else {
                        (None, None)
                    };

                    trajectories.push(TrajectoryPoint {
                        timestamp: live_status::current_timestamp(),
                        ekf_x: ekf_state_ref.position_local.0,
                        ekf_y: ekf_state_ref.position_local.1,
                        ekf_velocity: ekf_state_ref.velocity,
                        ekf_heading_deg: ekf_state_ref.heading_deg,
                        comp_velocity: comp_state.as_ref().map(|c| c.velocity).unwrap_or(0.0),
                        lat: lat_opt,
                        lon: lon_opt,
                        valid: origin_ready,
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
                    gravity_magnitude: (gravity_bias.0 * gravity_bias.0
                        + gravity_bias.1 * gravity_bias.1
                        + gravity_bias.2 * gravity_bias.2)
                        .sqrt(),
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
                origin_lat: origin_latlon.as_ref().map(|o| o.0),
                origin_lon: origin_latlon.as_ref().map(|o| o.1),
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
                let corrected_mag = (corrected_vec.x * corrected_vec.x
                    + corrected_vec.y * corrected_vec.y
                    + corrected_vec.z * corrected_vec.z)
                    .sqrt();
                let _smoothed_mag = accel_smoother.apply(corrected_mag);

                let reading = SensorReading {
                    timestamp: accel.timestamp,
                    accel: Some(accel.clone()),
                    gyro: None,
                    mag: sensor_state.latest_mag.read().await.clone(),
                    baro: sensor_state.latest_baro.read().await.clone(),
                    gps: None,
                    roughness: Some(avg_roughness),
                    specific_power_w_per_kg: 0.0,
                    power_coefficient: 0.0,
                    experimental_15d: None,
                    fgo: None,
                };

                log_jsonl_reading(&mut session_logger, &reading, &mut jsonl_count)?;
                readings.push(reading);

                if args.filter == "ekf" || args.filter == "both" {
                    let _ = ekf.update_accelerometer_vector(
                        corrected_vec.x,
                        corrected_vec.y,
                        corrected_vec.z,
                    );
                }
                if args.filter == "complementary" || args.filter == "both" {
                    let _ = comp_filter.update(
                        corrected_vec.x,
                        corrected_vec.y,
                        corrected_vec.z,
                        0.0,
                        0.0,
                        0.0,
                    );
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
            gravity_magnitude: (gravity_bias.0 * gravity_bias.0
                + gravity_bias.1 * gravity_bias.1
                + gravity_bias.2 * gravity_bias.2)
                .sqrt(),
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
        origin_lat: origin_latlon.map(|o| o.0),
        origin_lon: origin_latlon.map(|o| o.1),
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
