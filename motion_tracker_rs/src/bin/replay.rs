use std::fs::{self, File};
use std::io::{BufReader, Write};
use std::path::{Path, PathBuf};

use clap::Parser;
use flate2::read::GzDecoder;
use flate2::write::GzEncoder;
use flate2::Compression;
use motion_tracker_rs::sensor_fusion::{FusionConfig, SensorFusion};
use serde::Deserialize;
use serde_json::Value;
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

    /// GPS decimation for simulated denial testing (1=all, 10=10% coverage, 20=5% coverage)
    #[arg(long, default_value = "1")]
    gps_decimation: u32,

    /// Pure dead reckoning: use only first GPS fix for initialization, then IMU-only
    #[arg(long, default_value_t = false)]
    gps_init_only: bool,
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

fn recompute_and_write_roughness(path: &Path, output_dir: Option<&Path>) -> anyhow::Result<()> {
    let mut value = load_log_value(path)?;
    let readings = value
        .get_mut("readings")
        .and_then(|r| r.as_array_mut())
        .ok_or_else(|| anyhow::anyhow!("missing readings"))?;

    let mut est = RoughnessEstimator::new(50, 0.1);
    let mut _last_gps_speed = 0.0;
    let mut current_rough = 0.0;
    let mut first_accel_idx: Option<usize> = None;

    for (idx, r) in readings.iter_mut().enumerate() {
        if let Some(gps) = r.get("gps").and_then(|g| g.as_object()) {
            if let Some(spd) = gps.get("speed").and_then(|v| v.as_f64()) {
                _last_gps_speed = spd;
            }
        }
        if let Some(accel) = r.get("accel").and_then(|a| a.as_object()) {
            if let (Some(ax), Some(ay), Some(az)) = (
                accel.get("x").and_then(|v| v.as_f64()),
                accel.get("y").and_then(|v| v.as_f64()),
                accel.get("z").and_then(|v| v.as_f64()),
            ) {
                if first_accel_idx.is_none() {
                    first_accel_idx = Some(idx);
                }
                current_rough = est.update(ax, ay, az);
            }
        }
        let value_to_store = current_rough;
        r.as_object_mut()
            .expect("reading should be object")
            .insert("roughness".to_string(), serde_json::Value::from(value_to_store));
    }

    if let Some(accel_idx) = first_accel_idx {
        if let Some(first_val) = readings
            .get(accel_idx)
            .and_then(|r| r.get("roughness"))
            .and_then(|v| v.as_f64())
        {
            for r in readings.iter_mut().take(accel_idx) {
                r.as_object_mut()
                    .expect("reading should be object")
                    .insert("roughness".to_string(), serde_json::Value::from(first_val));
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
        let hx = self.hp_x.filter(ax);
        let hy = self.hp_y.filter(ay);
        let hz = self.hp_z.filter(az);

        let vib_sq = hx * hx + hy * hy + hz * hz;
        self.window.push_back(vib_sq);
        if self.window.len() > self.window_size {
            self.window.pop_front();
        }

        let rms = (self.window.iter().sum::<f64>() / self.window.len().max(1) as f64).sqrt();
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

fn latlon_to_enu(lat: f64, lon: f64, ref_lat: f64, ref_lon: f64) -> (f64, f64) {
    let dlat = (lat - ref_lat).to_radians();
    let dlon = (lon - ref_lon).to_radians();
    let east = dlon * 6371000.0 * ref_lat.to_radians().cos();
    let north = dlat * 6371000.0;
    (east, north)
}

fn local_to_global(lat_ref: f64, lon_ref: f64, north: f64, east: f64) -> (f64, f64) {
    const R: f64 = 6371000.0;
    let d_lat = north / R;
    let d_lon = east / (R * lat_ref.to_radians().cos());
    (
        lat_ref + d_lat.to_degrees(),
        lon_ref + d_lon.to_degrees()
    )
}

fn rmse_vec(errors_sq: &[f64]) -> f64 {
    if errors_sq.is_empty() {
        return f64::INFINITY;
    }
    (errors_sq.iter().sum::<f64>() / errors_sq.len() as f64).sqrt()
}

// ---------------------------------------------------------------------------
// Calibration helper: derive gravity & gyro bias from the first N stationary
// samples at the start of the session log.
// ---------------------------------------------------------------------------
fn calibrate_from_warmup(readings: &[Reading]) -> ((f64, f64, f64), (f64, f64, f64)) {
    let mut ax_sum = 0.0;
    let mut ay_sum = 0.0;
    let mut az_sum = 0.0;
    let mut a_count = 0usize;
    let mut gx_sum = 0.0;
    let mut gy_sum = 0.0;
    let mut gz_sum = 0.0;
    let mut g_count = 0usize;

    // Collect the first ~50 stationary accel/gyro samples for calibration.
    // Two-pass approach:
    //   1. Find the first reading with accel data
    //   2. Collect up to 50 samples, stopping if accel magnitude deviates
    //      from gravity (indicating movement) or GPS shows speed > 1
    let mut found_imu = false;
    for r in readings.iter() {
        if r.accel.is_some() {
            found_imu = true;
        }
        if !found_imu {
            continue; // skip GPS-only readings at the start
        }
        if let Some(gps) = r.gps.as_ref() {
            if gps.speed > 1.0 && a_count > 0 {
                break;
            }
        }
        if let Some(a) = r.accel.as_ref() {
            let mag = (a.x * a.x + a.y * a.y + a.z * a.z).sqrt();
            // Only use samples near gravity magnitude (stationary)
            if mag < 9.0 || mag > 10.5 {
                if a_count > 5 {
                    break; // movement detected, stop collecting
                }
                continue; // skip this sample
            }
            ax_sum += a.x;
            ay_sum += a.y;
            az_sum += a.z;
            a_count += 1;
        }
        if let Some(g) = r.gyro.as_ref() {
            gx_sum += g.x;
            gy_sum += g.y;
            gz_sum += g.z;
            g_count += 1;
        }
        if a_count >= 50 && g_count >= 50 {
            break;
        }
    }

    let gravity = if a_count > 0 {
        (ax_sum / a_count as f64, ay_sum / a_count as f64, az_sum / a_count as f64)
    } else {
        (0.0, 0.0, 9.81)
    };
    let gyro_bias = if g_count > 0 {
        (gx_sum / g_count as f64, gy_sum / g_count as f64, gz_sum / g_count as f64)
    } else {
        (0.0, 0.0, 0.0)
    };
    (gravity, gyro_bias)
}

// ---------------------------------------------------------------------------
// Main replay loop — now uses SensorFusion
// ---------------------------------------------------------------------------
fn run_once(path: &Path, args: &Args) -> anyhow::Result<serde_json::Value> {
    let log = load_log(path)?;

    // Build FusionConfig from CLI args + defaults
    let config = FusionConfig {
        q_vel: args.q_vel,
        gps_vel_std: args.gps_vel_std,
        normal_clamp_scale: args.clamp_scale,
        normal_clamp_offset: args.clamp_offset,
        enable_mag: args.enable_mag,
        enable_baro: args.enable_baro,
        ..FusionConfig::default()
    };

    let mut fusion = SensorFusion::new(config);

    // Calibrate from warmup samples
    let (gravity, gyro_bias) = calibrate_from_warmup(&log.readings);
    fusion.set_biases(gravity, gyro_bias);

    // Replay-local tracking
    let mut replay_origin: Option<(f64, f64)> = None;
    let mut ref_latlon: Option<(f64, f64)> = None;
    let mut gps_counter = 0u32;
    let mut total_gps_fixes = 0u32;
    let mut gps_fixes_fed = 0u32;

    // RMSE accumulators
    let mut position_errors_sq: Vec<f64> = Vec::new();
    let mut velocity_errors_pre_sq: Vec<f64> = Vec::new();
    let mut velocity_errors_post_sq: Vec<f64> = Vec::new();

    // Predicted position RMSE: only includes GPS fixes where IMU prediction ran
    let mut predicted_position_errors_sq: Vec<f64> = Vec::new();
    let mut had_predict_since_last_gps = false;

    // NIS tracking (read from EKF after each fed GPS update)
    let mut nis_sum: f64 = 0.0;
    let mut nis_count: u32 = 0;
    let mut nis_min: f64 = f64::INFINITY;
    let mut nis_max: f64 = 0.0;
    let mut nis_values: Vec<f64> = Vec::new();

    // Speed / gap tracking
    let mut max_innov_norm = 0.0;
    let mut max_delta_v = 0.0;
    let mut max_gps_gap = 0.0;
    let mut last_fed_gps_ts: Option<f64> = None;
    let mut max_speed_ts = 0.0;
    let mut max_speed_val = 0.0;
    let mut gps_speeds: Vec<f64> = Vec::new();
    let mut ekf_speeds: Vec<f64> = Vec::new();
    let mut paired: Vec<(f64, f64)> = Vec::new();
    let mut peak_mem_mb = get_memory_mb();
    let mut sample_counter = 0u32;

    // Trajectory output
    let mut trajectory: Vec<serde_json::Value> = Vec::new();

    for r in &log.readings {
        // --- Feed accel through SensorFusion ---
        if let Some(acc) = r.accel.as_ref() {
            let accel_data = motion_tracker_rs::types::AccelData {
                timestamp: acc.timestamp,
                x: acc.x,
                y: acc.y,
                z: acc.z,
            };
            let _events = fusion.feed_accel(&accel_data);

            // tick() after each accel (ZUPT + gravity refinement)
            let _tick_events = fusion.tick();
        }

        // --- Feed gyro through SensorFusion ---
        if let Some(g) = r.gyro.as_ref() {
            let gyro_data = motion_tracker_rs::types::GyroData {
                timestamp: g.timestamp,
                x: g.x,
                y: g.y,
                z: g.z,
            };
            let _events = fusion.feed_gyro(&gyro_data);
            had_predict_since_last_gps = true;
        }

        // --- Feed mag through SensorFusion ---
        if let Some(m) = r.mag.as_ref() {
            let mag_data = motion_tracker_rs::types::MagData {
                timestamp: m.timestamp,
                x: m.x,
                y: m.y,
                z: m.z,
            };
            fusion.feed_mag(&mag_data);
        }

        // --- Feed baro through SensorFusion ---
        if let Some(b) = r.baro.as_ref() {
            if let Some(pressure) = b.get("pressure_hpa").and_then(|p| p.as_f64()) {
                let baro_data = motion_tracker_rs::types::BaroData {
                    timestamp: r.timestamp,
                    pressure_hpa: pressure,
                };
                fusion.feed_baro(&baro_data);
            }
        }

        // --- GPS: decimation + metrics (stays in replay) ---
        if let Some(gps) = r.gps.as_ref() {
            total_gps_fixes += 1;
            gps_counter += 1;

            // Set origin reference on first GPS
            if replay_origin.is_none() {
                replay_origin = Some((gps.latitude, gps.longitude));
                ref_latlon = Some((gps.latitude, gps.longitude));
            }

            // Pre-update metrics (ALL GPS fixes, BEFORE any fusion.feed_gps)
            if let Some((rlat, rlon)) = ref_latlon {
                let (gps_e, gps_n) = latlon_to_enu(gps.latitude, gps.longitude, rlat, rlon);
                let snap = fusion.get_snapshot();
                let ekf_e = snap.position.0; // East
                let ekf_n = snap.position.1; // North
                let pos_err_sq = (ekf_e - gps_e).powi(2) + (ekf_n - gps_n).powi(2);
                position_errors_sq.push(pos_err_sq);

                let vel_err_pre = snap.speed - gps.speed;
                velocity_errors_pre_sq.push(vel_err_pre.powi(2));

                // Only include in predicted RMSE if IMU prediction ran since last GPS
                if had_predict_since_last_gps {
                    predicted_position_errors_sq.push(pos_err_sq);
                }
            }

            // Decimation gate
            let gps_decimated = args.gps_decimation == 1
                || (gps_counter % args.gps_decimation == 0)
                || gps_counter == 1; // always feed first fix
            let gps_init_only_skip = args.gps_init_only && gps_counter > 1;

            if gps_decimated && !gps_init_only_skip {
                gps_fixes_fed += 1;

                // Capture pre-update velocity for innovation tracking
                let vx_before = fusion.ekf.state[3];
                let vy_before = fusion.ekf.state[4];
                let vz_before = fusion.ekf.state[5];
                let bearing_rad = gps.bearing.to_radians();
                let vx_meas = gps.speed * bearing_rad.sin();
                let vy_meas = gps.speed * bearing_rad.cos();
                let innov_x = vx_meas - vx_before;
                let innov_y = vy_meas - vy_before;
                let innov_norm = (innov_x * innov_x + innov_y * innov_y).sqrt();
                if innov_norm > max_innov_norm {
                    max_innov_norm = innov_norm;
                }

                // Feed GPS through SensorFusion
                let gps_data = motion_tracker_rs::types::GpsData {
                    timestamp: gps.timestamp,
                    latitude: gps.latitude,
                    longitude: gps.longitude,
                    speed: gps.speed,
                    bearing: gps.bearing,
                    accuracy: gps.accuracy,
                };
                let _events = fusion.feed_gps(&gps_data, gps.timestamp);

                // NIS: the EKF computes NIS internally during update_gps.
                // We need it from the return value. Since feed_gps doesn't
                // expose it directly, we'll compute a proxy from innovation.
                // For consistency with the old code, read from the EKF's
                // last NIS by checking paired speed differences.
                // Actually, let's track via the velocity delta (delta_v) which
                // is the observable metric the old code already used.

                // Post-update velocity error (FED fixes only)
                let ekf_speed_post = fusion.get_speed();
                let vel_err_post = ekf_speed_post - gps.speed;
                velocity_errors_post_sq.push(vel_err_post.powi(2));

                // Track delta_v
                let vx_after = fusion.ekf.state[3];
                let vy_after = fusion.ekf.state[4];
                let vz_after = fusion.ekf.state[5];
                let delta_v = ((vx_after - vx_before).powi(2)
                    + (vy_after - vy_before).powi(2)
                    + (vz_after - vz_before).powi(2))
                .sqrt();
                if delta_v > max_delta_v {
                    max_delta_v = delta_v;
                }

                // Track GPS gap between fed fixes
                if let Some(last) = last_fed_gps_ts {
                    let gap = gps.timestamp - last;
                    if gap > max_gps_gap {
                        max_gps_gap = gap;
                    }
                }
                last_fed_gps_ts = Some(gps.timestamp);
                had_predict_since_last_gps = false;

                gps_speeds.push(gps.speed);
                paired.push((fusion.get_speed(), gps.speed));
            }
        }

        // Track speed
        let cur_speed = fusion.get_speed();
        if cur_speed > max_speed_val {
            max_speed_val = cur_speed;
            max_speed_ts = r.timestamp;
        }
        ekf_speeds.push(cur_speed);

        // Memory tracking
        sample_counter = sample_counter.wrapping_add(1);
        if sample_counter % 50 == 0 {
            let cur_mem = get_memory_mb();
            if cur_mem > peak_mem_mb {
                peak_mem_mb = cur_mem;
            }
        }

        // Trajectory point
        let snap = fusion.get_snapshot();
        trajectory.push(json!({
            "timestamp": r.timestamp,
            "ekf_x": snap.position.0,
            "ekf_y": snap.position.1,
            "ekf_velocity": cur_speed,
            "ekf_heading_deg": snap.heading_deg,
        }));
    }

    // --- Compute final metrics ---
    let position_rmse_m = rmse_vec(&position_errors_sq);
    let predicted_position_rmse_m = rmse_vec(&predicted_position_errors_sq);
    let predicted_position_fixes = predicted_position_errors_sq.len();
    let velocity_rmse_pre_update_mps = rmse_vec(&velocity_errors_pre_sq);
    let velocity_rmse_post_update_mps = rmse_vec(&velocity_errors_post_sq);
    let rmse_val = if paired.is_empty() {
        f64::INFINITY
    } else {
        let sum_sq: f64 = paired.iter().map(|(a, b)| (a - b).powi(2)).sum();
        (sum_sq / paired.len() as f64).sqrt()
    };
    let max_ekf: f64 = ekf_speeds.iter().copied().fold(0.0_f64, |m, v| m.max(v));
    let max_gps: f64 = gps_speeds.iter().copied().fold(0.0_f64, |m, v| m.max(v));

    // NIS: We don't have direct NIS from SensorFusion (it's returned by
    // update_gps inside feed_gps). For now, use delta_v as proxy and mark
    // NIS as not tracked. In a future iteration, SensorFusion should expose NIS.
    let nis_avg = 0.0;
    let nis_median = 0.0;
    let nis_verdict = "NOT_TRACKED";
    let _ = (nis_sum, nis_count, nis_min, nis_max, nis_values); // suppress warnings

    Ok(json!({
        "log": path.display().to_string(),
        "q_vel": args.q_vel,
        "gps_vel_std": args.gps_vel_std,
        "clamp_scale": args.clamp_scale,
        "clamp_offset": args.clamp_offset,
        "clamp_interval": args.clamp_interval,
        "gps_decimation": args.gps_decimation,
        "total_gps_fixes": total_gps_fixes,
        "gps_fixes_fed": gps_fixes_fed,
        "ground_truth_gps_count": total_gps_fixes,
        "decimated_gps_count": gps_speeds.len(),
        "position_rmse_m": position_rmse_m,
        "predicted_position_rmse_m": predicted_position_rmse_m,
        "predicted_position_fixes": predicted_position_fixes,
        "velocity_rmse_pre_update_mps": velocity_rmse_pre_update_mps,
        "velocity_rmse_post_update_mps": velocity_rmse_post_update_mps,
        "rmse": rmse_val,
        "max_ekf": max_ekf,
        "max_gps": max_gps,
        "pairs": paired.len(),
        "gps_samples": gps_speeds.len(),
        "ekf_samples": ekf_speeds.len(),
        "max_innovation_norm": max_innov_norm,
        "max_delta_v": max_delta_v,
        "max_speed_ts": max_speed_ts,
        "max_gps_gap": max_gps_gap,
        "peak_memory_mb": peak_mem_mb,
        "final_memory_mb": get_memory_mb(),
        "nis_avg": nis_avg,
        "nis_median": nis_median,
        "nis_min": 0.0,
        "nis_max": 0.0,
        "nis_count": 0,
        "nis_verdict": nis_verdict,
        "predict_count": fusion.predict_count,
        "zupt_count": fusion.zupt_count,
        "trajectories": trajectory
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
                        if let Err(e) = recompute_and_write_roughness(&path, out_dir) {
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
            recompute_and_write_roughness(log, out_dir)?;
        }
        results.push(res);
    } else {
        anyhow::bail!("Provide --log or --golden-dir");
    }

    // Print summary to stderr
    eprintln!("\n=== Replay Summary ===");
    for result in &results {
        if let Some(log_name) = result.get("log").and_then(|v| v.as_str()) {
            let pos_rmse = result.get("position_rmse_m").and_then(|v| v.as_f64()).unwrap_or(0.0);
            let vel_pre = result.get("velocity_rmse_pre_update_mps").and_then(|v| v.as_f64()).unwrap_or(0.0);
            eprintln!(
                "  {} | pos_rmse={:.2}m vel_pre_rmse={:.2}m/s dec={}",
                log_name.split('/').last().unwrap_or(log_name),
                pos_rmse,
                vel_pre,
                result.get("gps_decimation").and_then(|v| v.as_u64()).unwrap_or(0),
            );
        }
    }

    println!("{}", serde_json::to_string_pretty(&results)?);
    Ok(())
}
