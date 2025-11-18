use anyhow::Result;
use chrono::Utc;
use clap::Parser;
use serde::{Deserialize, Serialize};
use std::sync::{Arc, Mutex};
use tokio::sync::mpsc;
use tokio::time::{sleep, Duration};

mod filters;
mod sensors;
mod incident;
mod live_status;

use filters::es_ekf::EsEkf;
use filters::complementary::ComplementaryFilter;
use sensors::{AccelData, GyroData, GpsData};

#[derive(Parser, Debug)]
#[command(name = "motion_tracker")]
#[command(about = "Rust motion tracker - EKF vs Complementary filter comparison", long_about = None)]
struct Args {
    /// Duration in seconds (0 = continuous)
    #[arg(value_name = "SECONDS", default_value = "0")]
    duration: u64,

    /// Enable gyroscope processing
    #[arg(long)]
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

#[derive(Serialize, Deserialize)]
struct ComparisonOutput {
    readings: Vec<SensorReading>,
    incidents: Vec<incident::Incident>,
    stats: Stats,
}

#[derive(Serialize, Deserialize)]
struct Stats {
    total_samples: usize,
    total_incidents: usize,
    ekf_velocity: f64,
    ekf_distance: f64,
    gps_fixes: u64,
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();

    println!("[{}] Motion Tracker RS Starting", ts_now());
    println!("  Duration: {} seconds (0=continuous)", args.duration);
    println!("  Enable Gyro: {}", args.enable_gyro);
    println!("  Filter Mode: {}", args.filter);
    println!("  Output Dir: {}", args.output_dir);

    // Create output directory
    std::fs::create_dir_all(&args.output_dir)?;

    // Gravity calibration: collect 20 stationary samples at startup
    println!("[{}] Calibrating gravity (collecting 20 samples)...", ts_now());
    let mut gravity_samples = Vec::new();
    let mut calibration_complete = false;
    let mut gravity_magnitude = 9.81;

    // Initialize filters and incident detection
    let mut ekf = EsEkf::new(
        0.05,      // dt = 50ms
        8.0,       // gps_noise_std
        0.5,       // accel_noise_std
        args.enable_gyro,
        0.0005,    // gyro_noise_std (from CLAUDE.md)
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

    // Spawn sensor collection tasks (hold handles to keep tasks alive)
    let _accel_handle = tokio::spawn(sensors::accel_loop(accel_tx.clone()));
    let _gyro_handle = tokio::spawn(sensors::gyro_loop(gyro_tx.clone(), args.enable_gyro));
    let _gps_handle = tokio::spawn(sensors::gps_loop(gps_tx.clone()));

    // Drop original senders so tasks only hold references
    drop(accel_tx);
    drop(gyro_tx);
    drop(gps_tx);

    // Sample counters
    let mut accel_count = 0u64;
    let mut gyro_count = 0u64;
    let mut gps_count = 0u64;

    // Main processing loop
    let start = Utc::now();
    let mut last_save = Utc::now();
    let mut last_status_update = Utc::now();

    println!("[{}] Starting data collection...", ts_now());

    loop {
        // Check if duration exceeded
        if args.duration > 0 {
            let elapsed = Utc::now().signed_duration_since(start);
            if elapsed.num_seconds() as u64 >= args.duration {
                println!("[{}] Duration reached, stopping...", ts_now());
                break;
            }
        }

        // Collect available sensor readings
        while let Ok(accel) = accel_rx.try_recv() {
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

            // GRAVITY CALIBRATION: Collect first N stationary samples (reduced to 10 for faster startup)
            if !calibration_complete {
                let raw_mag = (accel.x * accel.x + accel.y * accel.y + accel.z * accel.z).sqrt();
                gravity_samples.push(raw_mag);

                if gravity_samples.len() >= 10 {
                    // Calculate average gravity magnitude (should be ~9.81 m/s²)
                    gravity_magnitude = gravity_samples.iter().sum::<f64>() / gravity_samples.len() as f64;
                    calibration_complete = true;
                    println!("[{}] Gravity calibration complete: {:.3} m/s² ({} samples)",
                        ts_now(), gravity_magnitude, gravity_samples.len());
                    gravity_samples.clear();
                } else {
                    // Still calibrating, skip filter updates
                    accel_count += 1;
                    continue;
                }
            }

            // Process through filters with calibrated gravity
            let raw_accel_mag = (accel.x * accel.x + accel.y * accel.y + accel.z * accel.z).sqrt();
            let true_accel_mag = (raw_accel_mag - gravity_magnitude).abs(); // TRUE acceleration (subtract gravity)

            // Detect incidents using true acceleration
            let gps_speed = if let Some(last) = readings_clone.lock().unwrap().last() {
                last.gps.as_ref().map(|g| g.speed)
            } else {
                None
            };
            let (lat, lon) = if let Some(last) = readings_clone.lock().unwrap().last() {
                last.gps.as_ref().map(|g| (g.latitude, g.longitude)).unwrap_or((0.0, 0.0))
            } else {
                (0.0, 0.0)
            };

            if let Some(incident) = incident_detector.detect(true_accel_mag, 0.0, gps_speed, accel.timestamp, Some(lat), Some(lon)) {
                incidents.push(incident);
            }

            if args.filter == "ekf" || args.filter == "both" {
                let _ = ekf.update_accelerometer(true_accel_mag);
            }
            if args.filter == "complementary" || args.filter == "both" {
                let _ = comp_filter.update(accel.x, accel.y, accel.z, 0.0, 0.0, 0.0);
            }

            accel_count += 1;
        }

        while let Ok(gyro) = gyro_rx.try_recv() {
            if let Some(last) = readings.lock().unwrap().last_mut() {
                last.gyro = Some(gyro.clone());
            }

            // Detect swerving incidents with gyro
            let gps_speed = readings.lock().unwrap().last()
                .and_then(|r| r.gps.as_ref().map(|g| g.speed));
            let (lat, lon) = readings.lock().unwrap().last()
                .and_then(|r| r.gps.as_ref().map(|g| (g.latitude, g.longitude)))
                .unwrap_or((0.0, 0.0));

            if let Some(incident) = incident_detector.detect(0.0, gyro.z, gps_speed, gyro.timestamp, Some(lat), Some(lon)) {
                incidents.push(incident);
            }

            if args.filter == "ekf" || args.filter == "both" {
                let _ = ekf.update_gyroscope(gyro.x, gyro.y, gyro.z);
            }

            gyro_count += 1;
        }

        while let Ok(gps) = gps_rx.try_recv() {
            if let Some(last) = readings.lock().unwrap().last_mut() {
                last.gps = Some(gps.clone());
            }

            if args.filter == "ekf" || args.filter == "both" {
                let _ = ekf.update_gps(gps.latitude, gps.longitude, Some(gps.speed), Some(gps.accuracy));
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

            if let Some(ekf) = ekf_state.as_ref() {
                live_status.ekf_velocity = ekf.velocity;
                live_status.ekf_distance = ekf.distance;
                live_status.ekf_heading_deg = ekf.heading_deg;
            }
            if let Some(comp) = comp_state.as_ref() {
                live_status.comp_velocity = comp.velocity;
            }

            let status_path = format!("{}/live_status.json", args.output_dir);
            let _ = live_status.save(&status_path);
            last_status_update = now;
        }

        // Auto-save every 15 seconds
        if (now.signed_duration_since(last_save).num_seconds() as u64) >= 15 {
            let readings_lock = readings.lock().unwrap();
            let ekf_state = ekf.get_state();
            let output = ComparisonOutput {
                readings: readings_lock.clone(),
                incidents: incidents.clone(),
                stats: Stats {
                    total_samples: readings_lock.len(),
                    total_incidents: incidents.len(),
                    ekf_velocity: ekf_state.as_ref().map(|s| s.velocity).unwrap_or(0.0),
                    ekf_distance: ekf_state.as_ref().map(|s| s.distance).unwrap_or(0.0),
                    gps_fixes: ekf_state.as_ref().map(|s| s.gps_updates).unwrap_or(0),
                },
            };
            let filename = format!("{}/comparison_{}.json", args.output_dir, ts_now_clean());
            let json = serde_json::to_string_pretty(&output)?;
            std::fs::write(&filename, json)?;
            println!("[{}] Auto-saved {} samples, {} incidents to {}", ts_now(), readings_lock.len(), incidents.len(), filename);
            drop(readings_lock);
            last_save = now;
        }

        sleep(Duration::from_millis(1)).await;
    }

    // Final save
    let readings_lock = readings.lock().unwrap();
    let ekf_state = ekf.get_state();
    let comp_state = comp_filter.get_state();
    let uptime = Utc::now().signed_duration_since(start).num_seconds().max(0) as u64;

    let output = ComparisonOutput {
        readings: readings_lock.clone(),
        incidents: incidents.clone(),
        stats: Stats {
            total_samples: readings_lock.len(),
            total_incidents: incidents.len(),
            ekf_velocity: ekf_state.as_ref().map(|s| s.velocity).unwrap_or(0.0),
            ekf_distance: ekf_state.as_ref().map(|s| s.distance).unwrap_or(0.0),
            gps_fixes: ekf_state.as_ref().map(|s| s.gps_updates).unwrap_or(0),
        },
    };
    let filename = format!("{}/comparison_{}_final.json", args.output_dir, ts_now_clean());
    let json = serde_json::to_string_pretty(&output)?;
    std::fs::write(&filename, json)?;
    println!("[{}] Final save: {} samples, {} incidents to {}", ts_now(), readings_lock.len(), incidents.len(), filename);

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
