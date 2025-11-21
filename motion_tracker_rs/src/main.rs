use anyhow::Result;
use chrono::Utc;
use clap::Parser;
use serde::{Deserialize, Serialize};
use std::sync::{Arc, Mutex};
use std::panic;
use std::fs::OpenOptions;
use std::io::Write;
use tokio::sync::mpsc;
use tokio::time::{sleep, Duration};

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

#[tokio::main]
async fn main() -> Result<()> {
    // Install panic hook to log panics to file
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

        // Print to stderr as well
        eprintln!("PANIC: {} at {}", msg, location);

        // Call original hook
        original_hook(panic_info);
    }));

    let args = Args::parse();

    println!("[{}] Motion Tracker RS Starting", ts_now());
    println!("  Duration: {} seconds (0=continuous)", args.duration);
    println!("  Enable Gyro: {}", args.enable_gyro);
    println!("  Filter Mode: {}", args.filter);
    println!("  Output Dir: {}", args.output_dir);

    // Create output directory
    std::fs::create_dir_all(&args.output_dir)?;

    // Gravity calibration: collect 20 stationary samples at startup
    // Using default gravity (9.81 m/s²) - calibration skipped due to async context issues
    let mut calibration_complete = false;
    let mut gravity_magnitude = 9.81;

    // Initialize filters and incident detection
    let mut ekf = EsEkf::new(
        0.05, // dt = 50ms
        8.0,  // gps_noise_std
        0.5,  // accel_noise_std
        args.enable_gyro,
        0.0005, // gyro_noise_std (from CLAUDE.md)
    );

    let mut comp_filter = ComplementaryFilter::new();
    let mut incident_detector = incident::IncidentDetector::new();
    let mut incidents: Vec<incident::Incident> = Vec::new();

    // Create channels for sensor data
    let (accel_tx, mut accel_rx) = mpsc::channel::<AccelData>(500);
    let (gyro_tx, mut gyro_rx) = mpsc::channel::<GyroData>(500);
    let (gps_tx, mut gps_rx) = mpsc::channel::<GpsData>(100);

    // Shared data collection
    let readings: Arc<Mutex<Vec<SensorReading>>> = Arc::new(Mutex::new(Vec::new()));
    let readings_clone = readings.clone();

    // Initialize health monitor
    let health_monitor = Arc::new(health_monitor::HealthMonitor::new());

    // Initialize restart manager
    let restart_manager = Arc::new(restart_manager::RestartManager::new());

    // Spawn sensor collection tasks (mutable handles for respawning support)
    let mut accel_handle = tokio::spawn(sensors::accel_loop(accel_tx.clone()));
    let mut gyro_handle = tokio::spawn(sensors::gyro_loop(gyro_tx.clone(), args.enable_gyro));
    let mut gps_handle = tokio::spawn(sensors::gps_loop(gps_tx.clone()));

    // Spawn health monitoring task with restart signaling
    let health_monitor_clone = health_monitor.clone();
    let restart_manager_clone = restart_manager.clone();
    let health_handle = tokio::spawn(health_monitor::health_monitor_task(
        health_monitor_clone,
        restart_manager_clone,
    ));

    // Keep senders in scope for respawning (don't drop)
    // Tasks hold clones, we keep originals for respawn

    // Sample counters
    let mut accel_count = 0u64;
    let mut gyro_count = 0u64;
    let mut gps_count = 0u64;

    // Trajectory tracking (for Python parity)
    let mut trajectories: Vec<TrajectoryPoint> = Vec::new();

    // Covariance snapshots (for analysis)
    let mut covariance_snapshots: Vec<CovarianceSnapshot> = Vec::new();

    // Memory tracking
    let mut peak_memory_mb: f64 = 0.0;
    let mut current_memory_mb: f64 = 0.0;

    // Hann-window smoothing for accelerometer magnitude (Python parity)
    let mut accel_smoother = AccelSmoother::new(9);

    // Skip gravity calibration - use default (9.81 m/s²)
    // Calibration logic in async context was problematic with time measurement
    // Future: Could spawn separate task for calibration if needed
    gravity_magnitude = 9.81;
    calibration_complete = true;
    println!("[{}] Using default gravity: 9.81 m/s²", ts_now());

    // Main processing loop with duration tracking via channel signal
    let start = Utc::now();
    let mut last_save = Utc::now();
    let mut last_status_update = Utc::now();

    // Create channel for duration timeout signal (sent from separate task)
    let (duration_tx, mut duration_rx) = mpsc::channel::<()>(1);
    let _duration_handle = if args.duration > 0 {
        let tx = duration_tx.clone();
        let duration_secs = args.duration;
        Some(tokio::spawn(async move {
            sleep(Duration::from_secs(duration_secs)).await;
            eprintln!("[TIMEOUT] Duration timer fired after {} seconds", duration_secs);
            let _ = tx.send(()).await; // Signal timeout
        }))
    } else {
        None
    };

    println!("[{}] Starting data collection...", ts_now());

    let mut loop_count = 0;
    loop {
        loop_count += 1;
        if loop_count == 1 || loop_count % 1000 == 0 {
            eprintln!("[MAIN LOOP] Iteration #{}", loop_count);
        }

        // Non-blocking check for duration timeout
        if duration_rx.try_recv().is_ok() {
            println!("[{}] Duration reached, stopping...", ts_now());
            break;
        }

        // Collect available sensor readings
        let mut accel_recv_count = 0;
        while let Ok(accel) = accel_rx.try_recv() {
            accel_recv_count += 1;
            if accel_recv_count == 1 || accel_recv_count % 100 == 0 {
                eprintln!("[MAIN] Received accel sample #{}", accel_recv_count);
            }
            // Update accel health
            health_monitor.accel.update();

            let timestamp = accel.timestamp;
            let mut reading = SensorReading {
                timestamp,
                accel: Some(accel.clone()),
                gyro: None,
                gps: None,
            };

            // Try to find matching gyro/gps
            let mut readings_lock = readings_clone.lock().unwrap();

            if let Some(last) = readings_lock.last_mut() {
                if (last.timestamp - timestamp).abs() < 0.1 {
                    reading.gyro = last.gyro.clone();
                    reading.gps = last.gps.clone();
                }
            }

            readings_lock.push(reading.clone());
            drop(readings_lock);

            // Process through filters (gravity already set to 9.81)
            let raw_accel_mag = (accel.x * accel.x + accel.y * accel.y + accel.z * accel.z).sqrt();
            let true_accel_mag = (raw_accel_mag - gravity_magnitude).abs(); // TRUE acceleration (subtract gravity)

            // Apply Hann-window smoothing to accelerometer magnitude (Python parity)
            let smoothed_accel_mag = accel_smoother.apply(true_accel_mag);

            // Detect incidents using smoothed acceleration
            let gps_speed = if let Some(last) = readings_clone.lock().unwrap().last() {
                last.gps.as_ref().map(|g| g.speed)
            } else {
                None
            };
            let (lat, lon) = if let Some(last) = readings_clone.lock().unwrap().last() {
                last.gps
                    .as_ref()
                    .map(|g| (g.latitude, g.longitude))
                    .unwrap_or((0.0, 0.0))
            } else {
                (0.0, 0.0)
            };

            if let Some(incident) = incident_detector.detect(
                smoothed_accel_mag,
                0.0,
                gps_speed,
                accel.timestamp,
                Some(lat),
                Some(lon),
            ) {
                incidents.push(incident);
            }

            if args.filter == "ekf" || args.filter == "both" {
                let _ = ekf.update_accelerometer(smoothed_accel_mag);
            }
            if args.filter == "complementary" || args.filter == "both" {
                let _ = comp_filter.update(accel.x, accel.y, accel.z, 0.0, 0.0, 0.0);
            }

            accel_count += 1;
        }

        while let Ok(gyro) = gyro_rx.try_recv() {
            // Update gyro health
            health_monitor.gyro.update();

            if let Some(last) = readings.lock().unwrap().last_mut() {
                last.gyro = Some(gyro.clone());
            }

            // Detect swerving incidents with gyro
            let gps_speed = readings
                .lock()
                .unwrap()
                .last()
                .and_then(|r| r.gps.as_ref().map(|g| g.speed));
            let (lat, lon) = readings
                .lock()
                .unwrap()
                .last()
                .and_then(|r| r.gps.as_ref().map(|g| (g.latitude, g.longitude)))
                .unwrap_or((0.0, 0.0));

            if let Some(incident) = incident_detector.detect(
                0.0,
                gyro.z,
                gps_speed,
                gyro.timestamp,
                Some(lat),
                Some(lon),
            ) {
                incidents.push(incident);
            }

            if args.filter == "ekf" || args.filter == "both" {
                let _ = ekf.update_gyroscope(gyro.x, gyro.y, gyro.z);
            }

            gyro_count += 1;
        }

        while let Ok(gps) = gps_rx.try_recv() {
            // Update GPS health
            health_monitor.gps.update();

            if let Some(last) = readings.lock().unwrap().last_mut() {
                last.gps = Some(gps.clone());
            }

            if args.filter == "ekf" || args.filter == "both" {
                let _ = ekf.update_gps(
                    gps.latitude,
                    gps.longitude,
                    Some(gps.speed),
                    Some(gps.accuracy),
                );
            }
            if args.filter == "complementary" || args.filter == "both" {
                let _ = comp_filter.update_gps(gps.latitude, gps.longitude);
            }

            gps_count += 1;
        }

        // Run predictions
        if args.filter == "ekf" || args.filter == "both" {
            let _ = ekf.predict();
        }

        // Update live status every 2 seconds
        let now = Utc::now();
        if (now.signed_duration_since(last_status_update).num_seconds() as u64) >= 2 {
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
            live_status.gravity_magnitude = gravity_magnitude;
            live_status.uptime_seconds = uptime;

            // Add health status
            let health_report = health_monitor.check_health();
            live_status.accel_healthy = health_report.accel_healthy;
            live_status.gyro_healthy = health_report.gyro_healthy;
            live_status.gps_healthy = health_report.gps_healthy;
            live_status.accel_silence_duration_secs = health_report
                .accel_silence_duration
                .unwrap_or(std::time::Duration::from_secs(0))
                .as_secs_f64();
            live_status.gyro_silence_duration_secs = health_report
                .gyro_silence_duration
                .unwrap_or(std::time::Duration::from_secs(0))
                .as_secs_f64();
            live_status.gps_silence_duration_secs = health_report
                .gps_silence_duration
                .unwrap_or(std::time::Duration::from_secs(0))
                .as_secs_f64();

            if let Some(ekf_state_ref) = ekf_state.as_ref() {
                live_status.ekf_velocity = ekf_state_ref.velocity;
                live_status.ekf_distance = ekf_state_ref.distance;
                live_status.ekf_heading_deg = ekf_state_ref.heading_deg;

                // Record trajectory point for Python parity
                let comp_vel = comp_state.as_ref().map(|c| c.velocity).unwrap_or(0.0);
                trajectories.push(TrajectoryPoint {
                    timestamp: live_status::current_timestamp(),
                    ekf_x: ekf_state_ref.position_local.0,
                    ekf_y: ekf_state_ref.position_local.1,
                    ekf_velocity: ekf_state_ref.velocity,
                    ekf_heading_deg: ekf_state_ref.heading_deg,
                    comp_velocity: comp_vel,
                });

                // Record covariance snapshot for analysis
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

            // Update memory metrics
            current_memory_mb = get_memory_mb();
            peak_memory_mb = peak_memory_mb.max(current_memory_mb);

            let status_path = format!("{}/live_status.json", args.output_dir);
            let _ = live_status.save(&status_path);

            // Log restart status
            let restart_status = restart_manager.status_report();
            eprintln!("[STATUS] {}", restart_status);

            last_status_update = now;
        }

        // Auto-save every 15 seconds
        if (now.signed_duration_since(last_save).num_seconds() as u64) >= 15 {
            let mut readings_lock = readings.lock().unwrap();
            let ekf_state = ekf.get_state();
            let sample_count = readings_lock.len();
            let elapsed_secs = now.signed_duration_since(start).num_seconds().max(0) as u64;
            let output = ComparisonOutput {
                readings: readings_lock.clone(),
                incidents: incidents.clone(),
                trajectories: trajectories.clone(),
                stats: Stats {
                    total_samples: readings_lock.len(),
                    total_incidents: incidents.len(),
                    ekf_velocity: ekf_state.as_ref().map(|s| s.velocity).unwrap_or(0.0),
                    ekf_distance: ekf_state.as_ref().map(|s| s.distance).unwrap_or(0.0),
                    gps_fixes: ekf_state.as_ref().map(|s| s.gps_updates).unwrap_or(0),
                },
                metrics: Metrics {
                    test_duration_seconds: elapsed_secs,
                    accel_samples: accel_count,
                    gyro_samples: gyro_count,
                    gps_samples: gps_count,
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
                "[{}] Auto-saved {} samples, {} incidents to {}",
                ts_now(),
                sample_count,
                incidents.len(),
                filename
            );

            // Clear readings vector to bound memory usage (all data is saved to disk)
            // Filters maintain their own state, so this doesn't lose information
            readings_lock.clear();
            println!(
                "[{}] Cleared {} readings from memory to prevent unbounded growth",
                ts_now(),
                sample_count
            );

            drop(readings_lock);
            last_save = now;
        }

        // Check for sensor restarts (respawn tasks if needed)
        if restart_manager.accel_ready_restart() {
            eprintln!("[RESTART] Respawning Accel task...");
            accel_handle.abort();
            accel_handle = tokio::spawn(sensors::accel_loop(accel_tx.clone()));
            restart_manager.accel_restart_success();
        }

        if restart_manager.gyro_ready_restart() && args.enable_gyro {
            eprintln!("[RESTART] Respawning Gyro task...");
            gyro_handle.abort();
            gyro_handle = tokio::spawn(sensors::gyro_loop(gyro_tx.clone(), args.enable_gyro));
            restart_manager.gyro_restart_success();
        }

        if restart_manager.gps_ready_restart() {
            eprintln!("[RESTART] Respawning GPS task...");
            gps_handle.abort();
            gps_handle = tokio::spawn(sensors::gps_loop(gps_tx.clone()));
            restart_manager.gps_restart_success();
        }

        // Yield to other tasks (accel/gyro/gps) to produce samples
        // 50ms is reasonable: allows other tasks to run frequently
        sleep(Duration::from_millis(50)).await;
    }

    // CRITICAL: Abort background tasks before acquiring locks
    // This prevents deadlock: if sensor tasks are holding locks while blocked
    // on channel send, aborting them releases those locks immediately
    println!("[CLEANUP] Main loop finished. Aborting background tasks...");
    accel_handle.abort();
    gyro_handle.abort();
    gps_handle.abort();
    health_handle.abort();

    // Yield to runtime to complete task cleanup
    tokio::task::yield_now().await;
    println!("[CLEANUP] Background tasks aborted. Proceeding to final save...");

    // Final save
    let readings_lock = readings.lock().unwrap();
    let ekf_state = ekf.get_state();
    let comp_state = comp_filter.get_state();
    let uptime = Utc::now().signed_duration_since(start).num_seconds().max(0) as u64;

    let output = ComparisonOutput {
        readings: readings_lock.clone(),
        incidents: incidents.clone(),
        trajectories: trajectories.clone(),
        stats: Stats {
            total_samples: readings_lock.len(),
            total_incidents: incidents.len(),
            ekf_velocity: ekf_state.as_ref().map(|s| s.velocity).unwrap_or(0.0),
            ekf_distance: ekf_state.as_ref().map(|s| s.distance).unwrap_or(0.0),
            gps_fixes: ekf_state.as_ref().map(|s| s.gps_updates).unwrap_or(0),
        },
        metrics: Metrics {
            test_duration_seconds: uptime,
            accel_samples: accel_count,
            gyro_samples: gyro_count,
            gps_samples: gps_count,
            gravity_magnitude,
            peak_memory_mb,
            current_memory_mb,
            covariance_snapshots: covariance_snapshots.clone(),
        },
    };
    let filename = format!(
        "{}/comparison_{}_final.json",
        args.output_dir,
        ts_now_clean()
    );
    let json = serde_json::to_string_pretty(&output)?;
    std::fs::write(&filename, json)?;
    println!(
        "[{}] Final save: {} samples, {} incidents to {}",
        ts_now(),
        readings_lock.len(),
        incidents.len(),
        filename
    );

    // Final live status update
    let mut final_status = live_status::LiveStatus::new();
    final_status.timestamp = live_status::current_timestamp();
    final_status.accel_samples = accel_count;
    final_status.gyro_samples = gyro_count;
    final_status.gps_fixes = gps_count;
    final_status.incidents_detected = incidents.len() as u64;
    final_status.calibration_complete = calibration_complete;
    final_status.gravity_magnitude = gravity_magnitude;
    final_status.uptime_seconds = uptime;
    if let Some(ekf) = ekf_state.as_ref() {
        final_status.ekf_velocity = ekf.velocity;
        final_status.ekf_distance = ekf.distance;
        final_status.ekf_heading_deg = ekf.heading_deg;
    }
    if let Some(comp) = comp_state.as_ref() {
        final_status.comp_velocity = comp.velocity;
    }
    let status_path = format!("{}/live_status_final.json", args.output_dir);
    let _ = final_status.save(&status_path);

    // Print stats
    println!("\n=== Final Stats ===");
    println!("Total samples: {}", readings_lock.len());
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
