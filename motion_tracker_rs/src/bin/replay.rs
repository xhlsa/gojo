use std::fs::File;
use std::io::BufReader;
use std::path::PathBuf;

use clap::Parser;
use flate2::read::GzDecoder;
use motion_tracker_rs::filters::ekf_15d::Ekf15d;
use serde::Deserialize;
use serde_json::json;

#[derive(Parser, Debug)]
struct Args {
    /// Path to comparison_*.json[.gz] log
    #[arg(long)]
    log: PathBuf,

    /// Velocity process noise (q_vel)
    #[arg(long, default_value = "0.5")]
    q_vel: f64,

    /// GPS velocity std (meters/sec)
    #[arg(long, default_value = "0.5")]
    gps_vel_std: f64,
}

#[derive(Deserialize)]
struct GpsData {
    timestamp: f64,
    latitude: f64,
    longitude: f64,
    speed: f64,
    bearing: f64,
    accuracy: f64,
}

#[derive(Deserialize)]
struct AccelData {
    timestamp: f64,
    x: f64,
    y: f64,
    z: f64,
}

#[derive(Deserialize)]
struct GyroData {
    timestamp: f64,
    x: f64,
    y: f64,
    z: f64,
}

#[derive(Deserialize)]
struct Reading {
    timestamp: f64,
    accel: Option<AccelData>,
    gyro: Option<GyroData>,
    gps: Option<GpsData>,
}

#[derive(Deserialize)]
struct LogFile {
    readings: Vec<Reading>,
}

fn load_log(path: &PathBuf) -> anyhow::Result<LogFile> {
    let file = File::open(path)?;
    if path.extension().map(|e| e == "gz").unwrap_or(false) {
        let gz = GzDecoder::new(file);
        let reader = BufReader::new(gz);
        Ok(serde_json::from_reader(reader)?)
    } else {
        let reader = BufReader::new(file);
        Ok(serde_json::from_reader(reader)?)
    }
}

fn main() -> anyhow::Result<()> {
    let args = Args::parse();
    let log = load_log(&args.log)?;

    // dt set to 0.02s (50 Hz) by default; adjust if your log differs
    let mut ekf = Ekf15d::new(0.02, 8.0, 0.5, 0.0005);
    // Override velocity process noise
    for i in 3..6 {
        ekf.process_noise[[i, i]] = args.q_vel;
    }

    let mut ekf_speeds = Vec::new();
    let mut gps_speeds = Vec::new();
    let mut paired = Vec::new();

    for r in &log.readings {
        if let Some(acc) = r.accel.as_ref() {
            ekf.predict((acc.x, acc.y, acc.z), (0.0, 0.0, 0.0));
        }
        if let Some(g) = r.gyro.as_ref() {
            ekf.predict((0.0, 0.0, 0.0), (g.x, g.y, g.z));
            ekf.update_stationary_gyro((g.x, g.y, g.z));
        }
        if let Some(gps) = r.gps.as_ref() {
            ekf.update_gps((gps.latitude, gps.longitude, 0.0), gps.accuracy);
            ekf.update_gps_velocity(gps.speed, gps.bearing.to_radians(), args.gps_vel_std);
            gps_speeds.push(gps.speed);
            paired.push((ekf.get_speed(), gps.speed));
        }
        ekf_speeds.push(ekf.get_speed());
    }

    let rmse_val = rmse_pairs(&paired);
    let max_ekf: f64 = ekf_speeds.iter().copied().fold(0.0_f64, |m, v| m.max(v));
    let max_gps: f64 = gps_speeds.iter().copied().fold(0.0_f64, |m, v| m.max(v));

    let out = json!({
        "log": args.log.display().to_string(),
        "q_vel": args.q_vel,
        "gps_vel_std": args.gps_vel_std,
        "rmse": rmse_val,
        "max_ekf": max_ekf,
        "max_gps": max_gps,
        "pairs": paired.len(),
        "gps_samples": gps_speeds.len(),
        "ekf_samples": ekf_speeds.len()
    });
    println!("{}", serde_json::to_string_pretty(&out)?);

    Ok(())
}

fn rmse_pairs(pairs: &[(f64, f64)]) -> f64 {
    if pairs.is_empty() {
        return f64::INFINITY;
    }
    let sum_sq: f64 = pairs.iter().map(|(a, b)| (a - b).powi(2)).sum();
    (sum_sq / pairs.len() as f64).sqrt()
}
