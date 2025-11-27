use std::fs::{self, File};
use std::io::{BufReader, Write};
use std::path::{Path, PathBuf};

use clap::Parser;
use flate2::read::GzDecoder;
use flate2::write::GzEncoder;
use flate2::Compression;
use motion_tracker_rs::filters::ekf_15d::Ekf15d;
use serde::Deserialize;
use serde_json::Value;
use motion_tracker_rs::types;
use serde_json::json;
use std::collections::VecDeque;

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

    /// Recompute roughness from raw accel using high-pass RMS (ignores logged roughness)
    #[arg(long, default_value_t = false)]
    recompute_roughness: bool,

    /// Dump recomputed roughness as CSV (timestamp,roughness) for tuning
    #[arg(long, default_value_t = false)]
    dump_roughness: bool,

    /// Write recomputed roughness back out to files (_rough.json.gz)
    #[arg(long, default_value_t = false)]
    write_roughness: bool,

    /// Output directory for written roughness files (defaults to golden/roughness_updated)
    #[arg(long)]
    output_dir: Option<PathBuf>,
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

fn load_log_value(path: &Path) -> anyhow::Result<Value> {
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

fn write_gz_json(value: &Value, path: &Path) -> anyhow::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let file = File::create(path)?;
    let mut encoder = GzEncoder::new(file, Compression::default());
    let data = serde_json::to_vec(value)?;
    encoder.write_all(&data)?;
    encoder.finish()?;
    Ok(())
}

fn recompute_and_write_roughness(
    path: &Path,
    output_dir: Option<&Path>,
    speed_gate: f64,
) -> anyhow::Result<()> {
    let mut value = load_log_value(path)?;
    let readings = value
        .get_mut("readings")
        .and_then(|r| r.as_array_mut())
        .ok_or_else(|| anyhow::anyhow!("missing readings"))?;

    let mut est = RoughnessEstimator::new(50, 0.1);
    let mut last_gps_speed = 0.0;

    for r in readings.iter_mut() {
        if let Some(gps) = r.get("gps").and_then(|g| g.as_object()) {
            if let Some(spd) = gps.get("speed").and_then(|v| v.as_f64()) {
                last_gps_speed = spd;
            }
        }
        if let Some(accel) = r.get("accel").and_then(|a| a.as_object()) {
            if let (Some(ax), Some(ay), Some(az)) = (
                accel.get("x").and_then(|v| v.as_f64()),
                accel.get("y").and_then(|v| v.as_f64()),
                accel.get("z").and_then(|v| v.as_f64()),
            ) {
                let rough = est.update(ax, ay, az);
                let value_to_store = if last_gps_speed > speed_gate { rough } else { 0.0 };
                r.as_object_mut()
                    .expect("reading should be object")
                    .insert("roughness".to_string(), serde_json::Value::from(value_to_store));
            }
        }
    }

    let parent = output_dir
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| {
            path.parent()
                .map(|p| p.join("roughness_updated"))
                .unwrap_or_else(|| PathBuf::from("roughness_updated"))
        });
    let stem = path
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("output");
    let out_name = format!("{}_rough.json.gz", stem.trim_end_matches(".json"));
    let out_path = parent.join(out_name);

    write_gz_json(&value, &out_path)?;
    println!("[WRITE] {}", out_path.display());
    Ok(())
}

fn rmse_pairs(pairs: &[(f64, f64)]) -> f64 {
    if pairs.is_empty() {
        return f64::INFINITY;
    }
    let sum_sq: f64 = pairs.iter().map(|(a, b)| (a - b).powi(2)).sum();
    (sum_sq / pairs.len() as f64).sqrt()
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
    let mut roughness_estimator = RoughnessEstimator::new(50, 0.1); // 1s window @50Hz, light EWMA
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
        if args.recompute_roughness {
            if let Some(acc) = r.accel.as_ref() {
                let rough = roughness_estimator.update(acc.x, acc.y, acc.z);
                // Only log roughness during clear driving (filter out walking/stationary)
                if args.dump_roughness && last_gps_speed > 5.0 {
                    println!("ROUGHNESS,{:.3},{:.6}", r.timestamp, rough);
                }
            }
        }
        if let Some(g) = r.gyro.as_ref() {
            ekf.predict((0.0, 0.0, 0.0), (g.x, g.y, g.z));
            ekf.update_stationary_gyro((g.x, g.y, g.z));
        }
        // Gap detection once per reading
        let in_gps_gap = last_gps_ts
            .map(|ts| (r.timestamp - ts).max(0.0) > 3.0)
            .unwrap_or(true);

        if args.enable_mag && in_gps_gap {
            if let Some(m) = r.mag.as_ref() {
                _latest_mag = Some(m.clone());
                // Mag yaw assist during GPS gaps when moving
                if let Some(last) = last_gps_ts {
                    let gap = (r.timestamp - last).max(0.0);
                    if gap > 3.0 && ekf.get_speed() > 2.0 && last_gps_speed > 2.0 {
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
        if args.enable_baro && in_gps_gap {
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

    if args.write_roughness && !args.recompute_roughness {
        println!("Note: --write-roughness implies --recompute-roughness");
    }

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
                Ok(res) => {
                    if args.write_roughness {
                        let out_dir = args.output_dir.as_deref();
                        // Recompute roughness and write to _rough file using driving gate
                        if let Err(e) = recompute_and_write_roughness(&path, out_dir, 5.0) {
                            eprintln!("Failed to write roughness for {}: {}", path.display(), e);
                        }
                    }
                    results.push(res);
                }
                Err(e) => eprintln!("Failed {}: {}", path.display(), e),
            }
        }
    } else if let Some(log) = args.log.as_ref() {
        let res = run_once(log, &args)?;
        if args.write_roughness {
            let out_dir = args.output_dir.as_deref();
            recompute_and_write_roughness(log, out_dir, 5.0)?;
        }
        results.push(res);
    } else {
        anyhow::bail!("Provide --log or --golden-dir");
    }

    println!("{}", serde_json::to_string_pretty(&results)?);
    Ok(())
}
