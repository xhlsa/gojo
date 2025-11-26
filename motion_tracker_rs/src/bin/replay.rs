use std::collections::VecDeque;
use std::fs::{self, File};
use std::io::BufReader;
use std::path::{Path, PathBuf};

use clap::Parser;
use flate2::read::GzDecoder;
use motion_tracker_rs::filters::ekf_15d::Ekf15d;
use serde::Deserialize;
use serde_json::Value;
use motion_tracker_rs::types;
use serde_json::json;

#[derive(Parser, Debug)]
struct Args {
    /// Path to comparison_*.json[.gz] log
    #[arg(long, conflicts_with = "golden_dir")]
    log: Option<PathBuf>,

    /// Directory of golden logs to batch replay (processes comparison_*.json[.gz])
    #[arg(long)]
    golden_dir: Option<PathBuf>,

    /// Velocity process noise (q_vel)
    #[arg(long, default_value = "0.5")]
    q_vel: f64,

    /// GPS velocity std (meters/sec)
    #[arg(long, default_value = "0.3")]
    gps_vel_std: f64,

    /// Clamp scale multiplier on recent GPS speed
    #[arg(long, default_value = "1.5")]
    clamp_scale: f64,

    /// Clamp offset added after scaling
    #[arg(long, default_value = "5.0")]
    clamp_offset: f64,

    /// Minimum seconds between clamps
    #[arg(long, default_value = "0.5")]
    clamp_interval: f64,

    /// Enable magnetometer yaw assist during replay (A/B testing)
    #[arg(long, default_value_t = false)]
    enable_mag: bool,

    /// Enable barometer-assisted zero vertical velocity during replay (A/B testing)
    #[arg(long, default_value_t = false)]
    enable_baro: bool,
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

#[allow(dead_code)]
#[derive(Deserialize)]
struct AccelData {
    timestamp: f64,
    x: f64,
    y: f64,
    z: f64,
}

#[allow(dead_code)]
#[derive(Deserialize)]
struct GyroData {
    timestamp: f64,
    x: f64,
    y: f64,
    z: f64,
}

#[allow(dead_code)]
#[derive(Debug, Clone, Deserialize)]
struct MagData {
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
    mag: Option<MagData>,
    baro: Option<Value>,
    gps: Option<GpsData>,
}

#[derive(Deserialize)]
struct LogFile {
    readings: Vec<Reading>,
}

fn load_log(path: &Path) -> anyhow::Result<LogFile> {
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

fn rmse_pairs(pairs: &[(f64, f64)]) -> f64 {
    if pairs.is_empty() {
        return f64::INFINITY;
    }
    let sum_sq: f64 = pairs.iter().map(|(a, b)| (a - b).powi(2)).sum();
    (sum_sq / pairs.len() as f64).sqrt()
}

fn get_memory_mb() -> f64 {
    if let Ok(content) = fs::read_to_string("/proc/self/status") {
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

fn run_once(path: &Path, args: &Args) -> anyhow::Result<serde_json::Value> {
    let log = load_log(path)?;
    // dt set to 0.02s (50 Hz) by default; adjust if your log differs
    let mut ekf = Ekf15d::new(0.02, 8.0, 0.5, 0.0005);
    // Override velocity process noise
    for i in 3..6 {
        ekf.process_noise[[i, i]] = args.q_vel;
    }

    let mut ekf_speeds = Vec::new();
    let mut gps_speeds = Vec::new();
    let mut paired = Vec::new();
    let mut recent_gps: VecDeque<(f64, f64)> = VecDeque::new(); // (timestamp, speed)
    let window_sec = 10.0;
    let mut last_speed_clamp_ts: f64 = -1.0;
    let mut last_nhc_ts: f64 = -1.0;
    let mut max_innov_norm = 0.0;
    let mut max_delta_v = 0.0;
    let mut yaw_debug_lines = 0;
    let mut max_speed_ts = 0.0;
    let mut max_speed_val = 0.0;
    let mut clamp_count = 0u64;
    let mut last_gps_ts: Option<f64> = None;
    let mut last_gps_speed: f64 = 0.0;
    let mut _latest_mag: Option<MagData> = None;
    let mut max_gps_gap = 0.0;
    let mut in_gap_mode: bool = false;
    let mut last_baro: Option<(f64, f64)> = None; // (timestamp, pressure_hpa)
    let mut peak_mem_mb = get_memory_mb();
    let mut sample_counter = 0u32;
    let mut mag_fires: u64 = 0;
    let mut baro_fires: u64 = 0;

    for r in &log.readings {
        if let Some(acc) = r.accel.as_ref() {
            ekf.predict((acc.x, acc.y, acc.z), (0.0, 0.0, 0.0));
            // Gap-mode speed ceiling during GPS outages (per prediction clamp)
            if let Some(ts) = last_gps_ts {
                let gap = (r.timestamp - ts).max(0.0);
                if gap > 5.0 || (in_gap_mode && gap > 0.5) {
                    in_gap_mode = true;
                }
                if in_gap_mode {
                    let limit = if last_gps_speed < 1.0 {
                        2.0
                    } else if last_gps_speed < 5.0 {
                        last_gps_speed * 2.0 + 2.0
                    } else {
                        1.1 * last_gps_speed + 2.0
                    }
                    .max(2.0);
                    let ekf_speed = ekf.get_speed();
                    if ekf_speed > limit {
                        println!(
                            "[GAP CLAMP] t={:.1}s gap={:.1}s speed {:.1} -> limit {:.1}",
                            r.timestamp, gap, ekf_speed, limit
                        );
                        ekf.clamp_speed(limit);
                    }
                }
            } else {
                in_gap_mode = false;
            }
            // Apply NHC at reduced rate (1s) with gap-aware noise; disable after long gaps
            if last_nhc_ts < 0.0 || (r.timestamp - last_nhc_ts) >= 1.0 {
                let nhc_gap = last_gps_ts
                    .map(|ts| (r.timestamp - ts).max(0.0))
                    .unwrap_or(0.0);
                if nhc_gap <= 10.0 {
                    let nhc_r = (1.0 + nhc_gap * 0.5).min(5.0);
                    ekf.update_body_velocity(nalgebra::Vector3::zeros(), nhc_r);
                } else {
                    println!("[NHC SKIP] gap {:.1}s", nhc_gap);
                }
                last_nhc_ts = r.timestamp;
            }
        }
        sample_counter = sample_counter.wrapping_add(1);
        if sample_counter % 50 == 0 {
            let cur_mem = get_memory_mb();
            if cur_mem > peak_mem_mb {
                peak_mem_mb = cur_mem;
            }
        }
        if let Some(g) = r.gyro.as_ref() {
            ekf.predict((0.0, 0.0, 0.0), (g.x, g.y, g.z));
            ekf.update_stationary_gyro((g.x, g.y, g.z));
        }
        if args.enable_mag {
            if let Some(m) = r.mag.as_ref() {
                _latest_mag = Some(m.clone());
                // Mag yaw assist during GPS gaps (>5s) and when moving
                if let Some(last) = last_gps_ts {
                    let gap = (r.timestamp - last).max(0.0);
                    if gap > 5.0 && ekf.get_speed() > 2.0 {
                        // Tilt compensation based on EKF attitude (roll/pitch from quaternion)
                        if let Some(yaw_correction) = ekf.update_mag_heading(
                            &crate::types::MagData {
                                timestamp: m.timestamp,
                                x: m.x,
                                y: m.y,
                                z: m.z,
                            },
                            0.157, // ~9° declination (Tucson)
                        ) {
                            println!(
                                "[MAG] gap {:.1}s yaw correction: {:.1}°",
                                gap,
                                yaw_correction.to_degrees()
                            );
                            mag_fires += 1;
                        }
                    }
                }
            }
        }
        if args.enable_baro {
            if let Some(baro_val) = r.baro.as_ref() {
                // Extract pressure and timestamp (fallback to reading timestamp)
                if let Some(pressure_hpa) = baro_val
                    .get("pressure_hpa")
                    .and_then(|v| v.as_f64())
                {
                    let ts = baro_val
                        .get("timestamp")
                        .and_then(|v| v.as_f64())
                        .unwrap_or(r.timestamp);
                    if let Some((prev_ts, prev_p)) = last_baro {
                        let dt = (ts - prev_ts).max(1e-3);
                        let dp_dt_hpa = (pressure_hpa - prev_p) / dt;
                        let dp_dt_pa = dp_dt_hpa * 100.0;
                        let pressure_stable = dp_dt_pa.abs() < 0.5; // ~0.4 m/s vertical
                        // Gate by speed: only constrain while moving, to mirror runtime
                        let gate_speed = last_gps_speed; // use last GPS speed, not drifting EKF speed
                        if gate_speed > 1.0 {
                            let z_noise = if pressure_stable { 0.005 } else { 1.0 };
                            println!(
                                "[BARO] t={:.2}s gps_speed={:.2} stable={} noise={:.3}",
                                r.timestamp,
                                gate_speed,
                                pressure_stable,
                                z_noise
                            );
                            ekf.zero_vertical_velocity(z_noise);
                            baro_fires += 1;
                        }
                    }
                    last_baro = Some((ts, pressure_hpa));
                }
            }
        }
        if let Some(gps) = r.gps.as_ref() {
            let vx_before = ekf.state[3];
            let vy_before = ekf.state[4];
            let vz_before = ekf.state[5];
            let speed_before = (vx_before * vx_before + vy_before * vy_before + vz_before * vz_before).sqrt();
            let bearing_rad = gps.bearing.to_radians();
            let vx_meas = gps.speed * bearing_rad.sin();
            let vy_meas = gps.speed * bearing_rad.cos();
            let innov_x = vx_meas - vx_before;
            let innov_y = vy_meas - vy_before;
            let innov_norm = (innov_x * innov_x + innov_y * innov_y).sqrt();
            if innov_norm > max_innov_norm {
                max_innov_norm = innov_norm;
            }

            // Yaw debug and forcing: target yaw = 90° - bearing (ENU CCW)
            if gps.speed > 5.0 {
                let target_yaw = std::f64::consts::FRAC_PI_2 - bearing_rad;
                // Extract current yaw
                let qw = ekf.state[6];
                let qx = ekf.state[7];
                let qy = ekf.state[8];
                let qz = ekf.state[9];
                let siny_cosp = 2.0 * (qw * qz + qx * qy);
                let cosy_cosp = 1.0 - 2.0 * (qy * qy + qz * qz);
                let yaw_before = siny_cosp.atan2(cosy_cosp);

                // Yaw measurement update (scalar)
                let mut innov = target_yaw - yaw_before;
                while innov > std::f64::consts::PI {
                    innov -= 2.0 * std::f64::consts::PI;
                }
                while innov < -std::f64::consts::PI {
                    innov += 2.0 * std::f64::consts::PI;
                }
                let _r_yaw = 0.1; // rad^2
                // Simple scalar Kalman update on yaw, assuming small-angle approx on quaternion z component
                // For robustness, just overwrite quaternion with target yaw (as measurement) and skip cov math here
                let half = target_yaw * 0.5;
                ekf.state[6] = half.cos();
                ekf.state[7] = 0.0;
                ekf.state[8] = 0.0;
                ekf.state[9] = half.sin();

                let siny_cosp2 = 2.0 * (ekf.state[6] * ekf.state[9] + ekf.state[7] * ekf.state[8]);
                let cosy_cosp2 = 1.0 - 2.0 * (ekf.state[8] * ekf.state[8] + ekf.state[9] * ekf.state[9]);
                let yaw_after = siny_cosp2.atan2(cosy_cosp2);

                if yaw_debug_lines < 10 {
                    println!(
                        "[YAW ALIGN] bearing={:.1}° target={:.1}° before={:.1}° after={:.1}° innov={:.2} rad",
                        gps.bearing,
                        target_yaw.to_degrees(),
                        yaw_before.to_degrees(),
                        yaw_after.to_degrees(),
                        innov
                    );
                    yaw_debug_lines += 1;
                }
            }

            ekf.update_gps((gps.latitude, gps.longitude, 0.0), gps.accuracy);
            // Fixed GPS velocity std
            ekf.update_gps_velocity(gps.speed, gps.bearing.to_radians(), args.gps_vel_std);
            // Clamp vertical velocity aggressively for land vehicle
            ekf.zero_vertical_velocity(1e-4);

            // Track GPS gap and log errors after post-update velocity is available
            let vx_after = ekf.state[3];
            let vy_after = ekf.state[4];
            let vz_after = ekf.state[5];
            let delta_v = ((vx_after - vx_before).powi(2) + (vy_after - vy_before).powi(2)).sqrt();
            if delta_v > max_delta_v {
                max_delta_v = delta_v;
            }
            let speed_after = (vx_after * vx_after + vy_after * vy_after + vz_after * vz_after).sqrt();
            if yaw_debug_lines < 5 {
                println!(
                    "[GPS_VEL] t={:.1} pre=({:.1},{:.1},{:.1}) |{:.1}| innov=({:.1},{:.1}) post=({:.1},{:.1},{:.1}) |{:.1}|",
                    gps.timestamp,
                    vx_before, vy_before, vz_before, speed_before,
                    innov_x, innov_y,
                    vx_after, vy_after, vz_after, speed_after
                );
            }

            // Track GPS gap
            if let Some(last) = last_gps_ts {
                let gap = gps.timestamp - last;
                if gap > max_gps_gap {
                    max_gps_gap = gap;
                }
                // Per-fix error logging (pre/post vs GPS ENU velocity)
                let err_pre =
                    ((vx_before - vx_meas).powi(2) + (vy_before - vy_meas).powi(2)).sqrt();
                let err_post =
                    ((vx_after - vx_meas).powi(2) + (vy_after - vy_meas).powi(2)).sqrt();
                println!(
                    "[ERR] t={:.1} gap={:.2}s err_pre={:.2} err_post={:.2} gps_speed={:.1}",
                    gps.timestamp,
                    gap,
                    err_pre,
                    err_post,
                    gps.speed
                );
            }
            last_gps_ts = Some(gps.timestamp);
            last_gps_speed = gps.speed;

            // Track recent GPS speeds for sanity gate
            recent_gps.push_back((gps.timestamp, gps.speed));
            while let Some((ts, _)) = recent_gps.front() {
                if gps.timestamp - *ts > window_sec {
                    recent_gps.pop_front();
                } else {
                    break;
                }
            }

            gps_speeds.push(gps.speed);
            paired.push((ekf.get_speed(), gps.speed));
        }

        // Velocity sanity gate based on recent GPS envelope; tighten during long GPS gaps
        if let Some(max_gps) = recent_gps.iter().map(|(_, s)| *s).max_by(|a, b| a.partial_cmp(b).unwrap()) {
            if max_gps > 3.0 {
                let ekf_speed = ekf.get_speed();
                let gap_for_clamp = last_gps_ts.map(|ts| (r.timestamp - ts).max(0.0)).unwrap_or(f64::INFINITY);
                let (scale, offset, min_interval) = if gap_for_clamp > 5.0 {
                    (1.0, 3.0, 0.0)
                } else {
                    (1.5, 5.0, 0.25)
                };
                let limit = scale * max_gps + offset;
                if ekf_speed > limit && ekf_speed > 1e-3 && (r.timestamp - last_speed_clamp_ts) > min_interval {
                    println!(
                        "[CLAMP] t={:.1}s gap={:.1}s speed {:.1} -> limit {:.1}",
                        r.timestamp,
                        gap_for_clamp,
                        ekf_speed,
                        limit
                    );
                    ekf.clamp_speed(limit);
                    last_speed_clamp_ts = r.timestamp;
                    clamp_count += 1;
                }
            }
        }
        let cur_speed = ekf.get_speed();
        if cur_speed > max_speed_val {
            max_speed_val = cur_speed;
            max_speed_ts = r.timestamp;
        }
        ekf_speeds.push(cur_speed);
    }

    let rmse_val = rmse_pairs(&paired);
    let max_ekf: f64 = ekf_speeds.iter().copied().fold(0.0_f64, |m, v| m.max(v));
    let max_gps: f64 = gps_speeds.iter().copied().fold(0.0_f64, |m, v| m.max(v));

    Ok(json!({
        "log": path.display().to_string(),
        "q_vel": args.q_vel,
        "gps_vel_std": args.gps_vel_std,
        "clamp_scale": args.clamp_scale,
        "clamp_offset": args.clamp_offset,
        "clamp_interval": args.clamp_interval,
        "rmse": rmse_val,
        "max_ekf": max_ekf,
        "max_gps": max_gps,
        "pairs": paired.len(),
        "gps_samples": gps_speeds.len(),
        "ekf_samples": ekf_speeds.len(),
        "max_innovation_norm": max_innov_norm,
        "max_delta_v": max_delta_v,
        "max_speed_ts": max_speed_ts,
        "clamp_count": clamp_count,
        "max_gps_gap": max_gps_gap,
        "mag_fires": mag_fires,
        "baro_fires": baro_fires,
        "peak_memory_mb": peak_mem_mb,
        "final_memory_mb": get_memory_mb()
    }))
}

fn main() -> anyhow::Result<()> {
    let args = Args::parse();
    let mut results = Vec::new();

    if let Some(dir) = args.golden_dir.as_ref() {
        for entry in std::fs::read_dir(dir)? {
            let entry = entry?;
            let path = entry.path();
            if !path.is_file() {
                continue;
            }
            let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
            if !(name.starts_with("comparison_") && (name.ends_with(".json") || name.ends_with(".json.gz"))) {
                continue;
            }
            match run_once(&path, &args) {
                Ok(res) => results.push(res),
                Err(e) => eprintln!("Failed {}: {}", path.display(), e),
            }
        }
    } else if let Some(log) = args.log.as_ref() {
        results.push(run_once(log, &args)?);
    } else {
        anyhow::bail!("Provide --log or --golden-dir");
    }

    println!("{}", serde_json::to_string_pretty(&results)?);
    Ok(())
}
