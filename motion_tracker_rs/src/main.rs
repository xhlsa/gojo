use anyhow::Result;
use chrono::Utc;
use clap::Parser;
use serde::{Deserialize, Serialize};
use std::collections::VecDeque;
use std::sync::{Arc, RwLock};
use std::panic;
use std::fs::OpenOptions;
use std::io::{Write, BufReader, BufRead};
use tokio::sync::mpsc;
use tokio::time::{sleep, Duration};
use std::process::{Command, Stdio};

mod filters;
mod health_monitor;
mod incident;
mod live_status;
mod restart_manager;
mod sensors;
mod smoothing;

use filters::complementary::ComplementaryFilter;
use filters::es_ekf::EsEkf;
use sensors::{AccelData, GpsData, GyroData};
use smoothing::AccelSmoother;

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
}

#[derive(Serialize, Deserialize, Clone)]
struct SensorReading {
    timestamp: f64,
    accel: Option<AccelData>,
    gyro: Option<GyroData>,
    gps: Option<GpsData>,
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
    peak_memory_mb: f64,
    current_memory_mb: f64,
    covariance_snapshots: Vec<CovarianceSnapshot>,
}

/// Shared sensor state using RwLock for minimal contention
#[derive(Clone)]
struct SensorState {
    pub accel_buffer: Arc<RwLock<VecDeque<AccelData>>>,
    pub gyro_buffer: Arc<RwLock<VecDeque<GyroData>>>,
    pub latest_accel: Arc<RwLock<Option<AccelData>>>,
    pub latest_gyro: Arc<RwLock<Option<GyroData>>>,
    pub accel_count: Arc<RwLock<u64>>,
    pub gyro_count: Arc<RwLock<u64>>,
}

impl SensorState {
    fn new() -> Self {
        Self {
            accel_buffer: Arc::new(RwLock::new(VecDeque::with_capacity(1024))),
            gyro_buffer: Arc::new(RwLock::new(VecDeque::with_capacity(1024))),
            latest_accel: Arc::new(RwLock::new(None)),
            latest_gyro: Arc::new(RwLock::new(None)),
            accel_count: Arc::new(RwLock::new(0u64)),
            gyro_count: Arc::new(RwLock::new(0u64)),
        }
    }
}

/// Sensor reader task: parse JSON and push to shared state
async fn accel_reader_task(state: SensorState) {
    eprintln!("[accel-reader] Initializing accelerometer reader");

    // Cleanup sensor
    let _ = Command::new("termux-sensor").arg("-c").output();
    sleep(Duration::from_millis(500)).await;

    // Start termux-sensor with reliable delay (-d 20 = ~50Hz)
    let mut child = match Command::new("termux-sensor")
        .arg("-d")
        .arg("20")  // ~50 Hz (empirically tested, reliable)
        .arg("-s")
        .arg("Accelerometer")  // Partial name works better than full sensor name
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
    {
        Ok(p) => {
            eprintln!("[accel-reader] Process spawned");
            p
        }
        Err(e) => {
            eprintln!("[accel-reader] Failed to spawn: {}", e);
            return;
        }
    };

    let stdout = match child.stdout.take() {
        Some(s) => s,
        None => {
            eprintln!("[accel-reader] No stdout");
            return;
        }
    };

    // Wrap blocking BufReader.lines() in spawn_blocking to avoid blocking tokio runtime
    let state_clone = state.clone();
    let _ = tokio::task::spawn_blocking(move || {
        let reader = BufReader::new(stdout);
        let mut line_count = 0u64;

        // Line-by-line reader (NOT streaming deserializer)
        for line in reader.lines() {
            match line {
                Ok(json_line) => {
                    // Parse JSON from line
                    if let Ok(obj) = serde_json::from_str::<serde_json::Value>(&json_line) {
                        if let Some(obj_map) = obj.as_object() {
                            for (sensor_key, sensor_data) in obj_map.iter() {
                                if sensor_key.contains("Accelerometer") {
                                    if let Some(values) = sensor_data.get("values").and_then(|v| v.as_array()) {
                                        if values.len() >= 3 {
                                            let accel = AccelData {
                                                timestamp: Utc::now().timestamp_millis() as f64 / 1000.0,
                                                x: values[0].as_f64().unwrap_or(0.0),
                                                y: values[1].as_f64().unwrap_or(0.0),
                                                z: values[2].as_f64().unwrap_or(0.0),
                                            };

                                            // Instantly acquire write lock, push, release
                                            {
                                                let mut buf = state_clone.accel_buffer.write().unwrap();
                                                if buf.len() > 1024 {
                                                    buf.pop_front();
                                                }
                                                buf.push_back(accel.clone());
                                            }

                                            // Update latest
                                            {
                                                let mut latest = state_clone.latest_accel.write().unwrap();
                                                *latest = Some(accel);
                                            }

                                            // Increment count
                                            {
                                                let mut count = state_clone.accel_count.write().unwrap();
                                                *count += 1;
                                            }

                                            line_count += 1;
                                            if line_count % 100 == 0 {
                                                eprintln!("[accel-reader] {} lines parsed", line_count);
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                Err(_) => break,
            }
        }

        eprintln!("[accel-reader] Stream ended after {} lines", line_count);
    })
    .await;
}

/// Gyro reader task: same pattern as accel
async fn gyro_reader_task(state: SensorState, enabled: bool) {
    if !enabled {
        return;
    }

    eprintln!("[gyro-reader] Initializing gyroscope reader");

    let mut child = match Command::new("termux-sensor")
        .arg("-d")
        .arg("20")  // ~50 Hz (reliable)
        .arg("-s")
        .arg("Gyroscope")  // Partial name works better than full sensor name
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
    {
        Ok(p) => p,
        Err(e) => {
            eprintln!("[gyro-reader] Failed to spawn: {}", e);
            return;
        }
    };

    let stdout = match child.stdout.take() {
        Some(s) => s,
        None => return,
    };

    // Wrap blocking BufReader.lines() in spawn_blocking
    let state_clone = state.clone();
    let _ = tokio::task::spawn_blocking(move || {
        let reader = BufReader::new(stdout);
        let mut line_count = 0u64;

        for line in reader.lines() {
            match line {
                Ok(json_line) => {
                    if let Ok(obj) = serde_json::from_str::<serde_json::Value>(&json_line) {
                        if let Some(obj_map) = obj.as_object() {
                            for (sensor_key, sensor_data) in obj_map.iter() {
                                if sensor_key.contains("Gyroscope") {
                                    if let Some(values) = sensor_data.get("values").and_then(|v| v.as_array()) {
                                        if values.len() >= 3 {
                                            let gyro = GyroData {
                                                timestamp: Utc::now().timestamp_millis() as f64 / 1000.0,
                                                x: values[0].as_f64().unwrap_or(0.0),
                                                y: values[1].as_f64().unwrap_or(0.0),
                                                z: values[2].as_f64().unwrap_or(0.0),
                                            };

                                            {
                                                let mut buf = state_clone.gyro_buffer.write().unwrap();
                                                if buf.len() > 1024 {
                                                    buf.pop_front();
                                                }
                                                buf.push_back(gyro.clone());
                                            }

                                            {
                                                let mut latest = state_clone.latest_gyro.write().unwrap();
                                                *latest = Some(gyro);
                                            }

                                            {
                                                let mut count = state_clone.gyro_count.write().unwrap();
                                                *count += 1;
                                            }

                                            line_count += 1;
                                            if line_count % 100 == 0 {
                                                eprintln!("[gyro-reader] {} lines parsed", line_count);
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                Err(_) => break,
            }
        }

        eprintln!("[gyro-reader] Stream ended after {} lines", line_count);
    })
    .await;
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

    // Spawn reader tasks (NOT filter tasks)
    let accel_state = sensor_state.clone();
    let accel_reader_handle = tokio::spawn(async move {
        accel_reader_task(accel_state).await;
    });

    let gyro_state = sensor_state.clone();
    let gyro_enabled = args.enable_gyro;
    let gyro_reader_handle = tokio::spawn(async move {
        gyro_reader_task(gyro_state, gyro_enabled).await;
    });

    // Initialize filters
    let mut ekf = EsEkf::new(0.05, 8.0, 0.5, args.enable_gyro, 0.0005);
    let mut comp_filter = ComplementaryFilter::new();
    let mut incident_detector = incident::IncidentDetector::new();
    let mut incidents: Vec<incident::Incident> = Vec::new();
    let mut readings: Vec<SensorReading> = Vec::new();
    let mut trajectories: Vec<TrajectoryPoint> = Vec::new();
    let mut covariance_snapshots: Vec<CovarianceSnapshot> = Vec::new();

    let mut peak_memory_mb: f64 = 0.0;
    let mut current_memory_mb: f64 = 0.0;
    let mut accel_smoother = AccelSmoother::new(9);
    let gravity_magnitude = 9.81;
    let calibration_complete = true;

    println!("[{}] Using default gravity: 9.81 m/s²", ts_now());

    let start = Utc::now();
    let mut last_save = Utc::now();
    let mut last_status_update = Utc::now();

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

    // Main loop: Consumer at fixed 20ms tick (50Hz)
    loop {
        // Check duration
        if duration_rx.try_recv().is_ok() {
            println!("[{}] Duration reached, stopping...", ts_now());
            break;
        }

        // Acquire read lock on accel buffer
        {
            let mut buf = sensor_state.accel_buffer.write().unwrap();
            while let Some(accel) = buf.pop_front() {
                let raw_mag = (accel.x * accel.x + accel.y * accel.y + accel.z * accel.z).sqrt();
                let true_mag = (raw_mag - gravity_magnitude).abs();
                let smoothed_mag = accel_smoother.apply(true_mag);

                let reading = SensorReading {
                    timestamp: accel.timestamp,
                    accel: Some(accel.clone()),
                    gyro: None,
                    gps: None,
                };

                readings.push(reading);

                if args.filter == "ekf" || args.filter == "both" {
                    let _ = ekf.update_accelerometer(smoothed_mag);
                }
                if args.filter == "complementary" || args.filter == "both" {
                    let _ = comp_filter.update(accel.x, accel.y, accel.z, 0.0, 0.0, 0.0);
                }
            }
        }

        // Acquire read lock on gyro buffer
        {
            let mut buf = sensor_state.gyro_buffer.write().unwrap();
            while let Some(gyro) = buf.pop_front() {
                if let Some(last) = readings.last_mut() {
                    last.gyro = Some(gyro.clone());
                }

                if args.filter == "ekf" || args.filter == "both" {
                    let _ = ekf.update_gyroscope(gyro.x, gyro.y, gyro.z);
                }
            }
        }

        // Run EKF prediction
        if args.filter == "ekf" || args.filter == "both" {
            let _ = ekf.predict();
        }

        // Status update every 2 seconds
        let now = Utc::now();
        if (now.signed_duration_since(last_status_update).num_seconds() as i64) >= 2i64 {
            let accel_count = *sensor_state.accel_count.read().unwrap();
            let gyro_count = *sensor_state.gyro_count.read().unwrap();

            let ekf_state = ekf.get_state();
            let comp_state = comp_filter.get_state();
            let uptime = now.signed_duration_since(start).num_seconds().max(0) as u64;

            let mut live_status = live_status::LiveStatus::new();
            live_status.timestamp = live_status::current_timestamp();
            live_status.accel_samples = accel_count;
            live_status.gyro_samples = gyro_count;
            live_status.gps_fixes = 0;
            live_status.incidents_detected = incidents.len() as u64;
            live_status.calibration_complete = calibration_complete;
            live_status.gravity_magnitude = gravity_magnitude;
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
            let accel_count = *sensor_state.accel_count.read().unwrap();
            let ekf_state = ekf.get_state();
            let elapsed_secs = now.signed_duration_since(start).num_seconds().max(0i64) as u64;
            let gyro_count = *sensor_state.gyro_count.read().unwrap();

            let output = ComparisonOutput {
                readings: readings.clone(),
                incidents: incidents.clone(),
                trajectories: trajectories.clone(),
                stats: Stats {
                    total_samples: readings.len(),
                    total_incidents: incidents.len(),
                    ekf_velocity: ekf_state.as_ref().map(|s| s.velocity).unwrap_or(0.0),
                    ekf_distance: ekf_state.as_ref().map(|s| s.distance).unwrap_or(0.0),
                    gps_fixes: 0,
                },
                metrics: Metrics {
                    test_duration_seconds: elapsed_secs,
                    accel_samples: accel_count,
                    gyro_samples: gyro_count,
                    gps_samples: 0,
                    gravity_magnitude,
                    peak_memory_mb,
                    current_memory_mb,
                    covariance_snapshots: covariance_snapshots.clone(),
                },
            };

            let filename = format!("{}/comparison_{}.json", args.output_dir, ts_now_clean());
            let json = serde_json::to_string_pretty(&output)?;
            std::fs::write(&filename, json)?;

            println!(
                "[{}] Auto-saved {} samples to {}",
                ts_now(),
                readings.len(),
                filename
            );

            readings.clear();
            last_save = now;
        }

        // Consumer tick: 20ms (50Hz)
        sleep(Duration::from_millis(20)).await;
    }

    // Abort reader tasks
    println!("[CLEANUP] Aborting reader tasks...");
    accel_reader_handle.abort();
    gyro_reader_handle.abort();
    tokio::task::yield_now().await;

    // Final save
    let accel_count = *sensor_state.accel_count.read().unwrap();
    let gyro_count = *sensor_state.gyro_count.read().unwrap();
    let ekf_state = ekf.get_state();
    let uptime = Utc::now().signed_duration_since(start).num_seconds().max(0) as u64;

    let output = ComparisonOutput {
        readings: readings.clone(),
        incidents: incidents.clone(),
        trajectories: trajectories.clone(),
        stats: Stats {
            total_samples: readings.len(),
            total_incidents: incidents.len(),
            ekf_velocity: ekf_state.as_ref().map(|s| s.velocity).unwrap_or(0.0),
            ekf_distance: ekf_state.as_ref().map(|s| s.distance).unwrap_or(0.0),
            gps_fixes: 0,
        },
        metrics: Metrics {
            test_duration_seconds: uptime,
            accel_samples: accel_count,
            gyro_samples: gyro_count,
            gps_samples: 0,
            gravity_magnitude,
            peak_memory_mb,
            current_memory_mb,
            covariance_snapshots: covariance_snapshots.clone(),
        },
    };

    let filename = format!("{}/comparison_{}_final.json", args.output_dir, ts_now_clean());
    let json = serde_json::to_string_pretty(&output)?;
    std::fs::write(&filename, json)?;

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
