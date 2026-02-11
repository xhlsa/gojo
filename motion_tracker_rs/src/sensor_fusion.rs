// sensor_fusion.rs — Pure computation layer for Gojo
//
// Everything in this module is independent of:
//   - tokio / async runtime
//   - Termux / termux-sensor / termux-location
//   - File I/O, dashboard, Rerun logging
//
// It takes sensor samples in, produces state estimates and events out.
// This means you can unit-test it with recorded data, replay .json.gz sessions,
// and swap the Termux frontend for a VectorNav or simulated data without touching fusion logic.

use nalgebra::Vector3;
use std::collections::VecDeque;

use crate::filters::complementary::{ComplementaryFilter, ComplementaryFilterState};
use crate::filters::ekf_13d::Ekf13d;
use crate::filters::ekf_15d::Ekf15d;
use crate::filters::es_ekf::EsEkf;
use crate::filters::fgo::GraphEstimator;
use crate::incident::{Incident, IncidentDetector};
use crate::smoothing::AccelSmoother;
use crate::types::{AccelData, BaroData, GpsData, GyroData, MagData};

// ─── Configuration ───────────────────────────────────────────────────────────

#[derive(Clone, Debug)]
pub struct FusionConfig {
    // ── Filter construction ──
    pub dt: f64,
    pub gps_noise: f64,
    pub accel_noise: f64,
    pub gyro_noise: f64,
    pub es_ekf_vel_noise: f64,

    // ── GPS velocity update ──
    pub gps_vel_std: f64,

    // ── Speed clamping ──
    pub normal_clamp_scale: f64,
    pub normal_clamp_offset: f64,
    pub gap_clamp_scale: f64,
    pub gap_clamp_offset: f64,
    pub gap_clamp_trigger: f64,
    pub gap_clamp_hyst: f64,

    // ── Low-pass filter on raw accel ──
    pub accel_lpf_cutoff_hz: f64,
    pub accel_lpf_sample_hz: f64,

    // ── ZUPT thresholds ──
    pub zupt_accel_low: f64,
    pub zupt_accel_high: f64,
    pub zupt_gyro_threshold: f64,

    // ── Incident detection ──
    pub brake_threshold: f64,
    pub turn_threshold: f64,
    pub crash_threshold: f64,
    pub incident_cooldown_secs: f64,

    // ── NHC ──
    pub nhc_interval_secs: f64,
    pub nhc_max_gap_secs: f64,

    // ── Magnetometer gating ──
    pub mag_min_speed: f64,
    pub mag_min_gps_gap: f64,
    pub mag_declination_rad: f64,

    // ── Barometer gating ──
    pub baro_min_speed: f64,
    pub baro_pressure_rate_threshold: f64,

    // ── GPS gating ──
    pub gps_max_accuracy: f64,
    pub gps_max_latency: f64,
    pub gps_max_projection_speed: f64,
    pub gps_speed_window: f64,
    pub gps_stationary_speed: f64,

    // ── Roughness estimator ──
    pub roughness_window_size: usize,
    pub roughness_ewma_alpha: f64,
    pub roughness_smooth_threshold: f64,

    // ── Dynamic gravity calibration ──
    pub dyn_calib_ema_alpha: f64,
    pub dyn_calib_min_samples: usize,
    pub dyn_calib_drift_threshold: f64,

    // ── Accel smoother ──
    pub accel_smoother_window: usize,

    // ── Gyro straight-road clamp ──
    pub gyro_straight_threshold: f64,
    pub gyro_straight_min_speed: f64,

    // ── Feature flags ──
    pub enable_gyro: bool,
    pub enable_mag: bool,
    pub enable_baro: bool,
    pub enable_fgo: bool,
    pub enable_13d: bool,
    pub enable_complementary: bool,
}

impl Default for FusionConfig {
    fn default() -> Self {
        Self {
            dt: 0.05,
            gps_noise: 8.0,
            accel_noise: 0.3,
            gyro_noise: 0.0005,
            es_ekf_vel_noise: 0.5,
            gps_vel_std: 0.3,
            normal_clamp_scale: 1.5,
            normal_clamp_offset: 5.0,
            gap_clamp_scale: 1.1,
            gap_clamp_offset: 2.0,
            gap_clamp_trigger: 5.0,
            gap_clamp_hyst: 0.5,
            accel_lpf_cutoff_hz: 4.0,
            accel_lpf_sample_hz: 50.0,
            zupt_accel_low: 9.5,
            zupt_accel_high: 10.1,
            zupt_gyro_threshold: 0.1,
            brake_threshold: 4.0,
            turn_threshold: 4.0,
            crash_threshold: 20.0,
            incident_cooldown_secs: 1.0,
            nhc_interval_secs: 1.0,
            nhc_max_gap_secs: 10.0,
            mag_min_speed: 2.0,
            mag_min_gps_gap: 3.0,
            mag_declination_rad: 0.157,
            baro_min_speed: 1.0,
            baro_pressure_rate_threshold: 0.5,
            gps_max_accuracy: 50.0,
            gps_max_latency: 1.0,
            gps_max_projection_speed: 50.0,
            gps_speed_window: 10.0,
            gps_stationary_speed: 0.5,
            roughness_window_size: 50,
            roughness_ewma_alpha: 0.1,
            roughness_smooth_threshold: 0.5,
            dyn_calib_ema_alpha: 0.1,
            dyn_calib_min_samples: 30,
            dyn_calib_drift_threshold: 0.5,
            accel_smoother_window: 9,
            gyro_straight_threshold: 0.02,
            gyro_straight_min_speed: 5.0,
            enable_gyro: true,
            enable_mag: false,
            enable_baro: false,
            enable_fgo: true,
            enable_13d: true,
            enable_complementary: true,
        }
    }
}

// ─── Events ──────────────────────────────────────────────────────────────────

#[derive(Clone, Debug)]
pub enum FusionEvent {
    SpeedClamped { from_speed: f64, to_limit: f64, gap_secs: f64 },
    GpsRejected { accuracy: f64, speed: f64 },
    ColdStartInitialized { lat: f64, lon: f64 },
    HeadingAligned { bearing_deg: f64, yaw_deg: f64, speed: f64 },
    HighGpsLatency { latency_secs: f64 },
    NhcSkipped { gap_secs: f64 },
    MagCorrection { gap_secs: f64, innovation_deg: f64 },
    GravityRefined { refinement_count: u64, estimate: (f64, f64, f64), magnitude: f64, drift: f64 },
    GravityDriftWarning { drift: f64, threshold: f64 },
    IncidentDetected(Incident),
    ZuptApplied,
    GapClampActive { gap_secs: f64, speed: f64, limit: f64 },
    GapModeExited,
    FgoOptimization { nodes: usize, gps_factors: usize, iteration: usize },
}

// ─── Fusion output snapshot ──────────────────────────────────────────────────

#[derive(Clone, Debug)]
pub struct FusionSnapshot {
    pub ekf_15d_state: crate::filters::ekf_15d::Ekf15dState,
    pub ekf_13d_state: Option<crate::filters::ekf_13d::Ekf13dState>,
    pub es_ekf_state: Option<crate::filters::es_ekf::EsEkfState>,
    pub comp_state: Option<ComplementaryFilterState>,
    pub fgo_state: Option<crate::filters::fgo::FgoState>,
    pub gravity_bias: (f64, f64, f64),
    pub gyro_bias: (f64, f64, f64),
    pub calibration_complete: bool,
    pub gravity_refinements: u64,
    pub gravity_drift: f64,
    pub roughness: f64,
    pub is_stationary: bool,
    pub in_gap_mode: bool,
    pub gps_gap_secs: f64,
    pub heading_initialized: bool,
}

// ─── Signal processing (moved from main.rs) ─────────────────────────────────

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
        Self { alpha, last_output: Vector3::zeros(), initialized: false }
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

struct HighPassFilter { x1: f64, x2: f64, y1: f64, y2: f64 }

impl HighPassFilter {
    fn new() -> Self { Self { x1: 0.0, x2: 0.0, y1: 0.0, y2: 0.0 } }

    fn filter(&mut self, x: f64) -> f64 {
        // 2nd-order Butterworth high-pass, 3 Hz @ 50 Hz
        const B: [f64; 3] = [0.8371, -1.6742, 0.8371];
        const A: [f64; 3] = [1.0, -1.6475, 0.7009];
        let y = B[0] * x + B[1] * self.x1 + B[2] * self.x2 - A[1] * self.y1 - A[2] * self.y2;
        self.x2 = self.x1; self.x1 = x;
        self.y2 = self.y1; self.y1 = y;
        y
    }
}

struct RoughnessEstimator {
    hp_x: HighPassFilter, hp_y: HighPassFilter, hp_z: HighPassFilter,
    window: VecDeque<f64>, window_size: usize, ewma: f64, alpha: f64,
}

impl RoughnessEstimator {
    fn new(window_size: usize, alpha: f64) -> Self {
        Self {
            hp_x: HighPassFilter::new(), hp_y: HighPassFilter::new(), hp_z: HighPassFilter::new(),
            window: VecDeque::with_capacity(window_size), window_size, ewma: 0.0, alpha,
        }
    }

    fn update(&mut self, ax: f64, ay: f64, az: f64) -> f64 {
        let hx = self.hp_x.filter(ax);
        let hy = self.hp_y.filter(ay);
        let hz = self.hp_z.filter(az);
        let vib_sq = hx * hx + hy * hy + hz * hz;
        self.window.push_back(vib_sq);
        if self.window.len() > self.window_size { self.window.pop_front(); }
        let rms = (self.window.iter().sum::<f64>() / self.window.len().max(1) as f64).sqrt();
        self.ewma = self.alpha * rms + (1.0 - self.alpha) * self.ewma;
        self.ewma
    }
}

struct IncidentCooldown { last_trigger: f64, cooldown_secs: f64 }

impl IncidentCooldown {
    fn new(cooldown_secs: f64) -> Self {
        Self { last_trigger: f64::NEG_INFINITY, cooldown_secs }
    }
    fn ready_and_touch(&mut self, now: f64) -> bool {
        if now - self.last_trigger >= self.cooldown_secs {
            self.last_trigger = now; true
        } else { false }
    }
}

// ─── Dynamic gravity calibration ─────────────────────────────────────────────

#[derive(Clone, Debug)]
struct DynamicCalibration {
    gravity_accumulator: Vec<(f64, f64, f64)>,
    pub gravity_estimate: (f64, f64, f64),
    gravity_startup: (f64, f64, f64),
    pub refinement_count: u64,
    ema_alpha: f64,
    min_samples: usize,
    pub drift_threshold: f64,
}

impl DynamicCalibration {
    fn new(initial_gravity: (f64, f64, f64), config: &FusionConfig) -> Self {
        Self {
            gravity_accumulator: Vec::with_capacity(100),
            gravity_estimate: initial_gravity,
            gravity_startup: initial_gravity,
            refinement_count: 0,
            ema_alpha: config.dyn_calib_ema_alpha,
            min_samples: config.dyn_calib_min_samples,
            drift_threshold: config.dyn_calib_drift_threshold,
        }
    }

    fn accumulate(&mut self, ax: f64, ay: f64, az: f64) {
        self.gravity_accumulator.push((ax, ay, az));
    }

    fn try_refine(&mut self) -> Option<(f64, f64, f64)> {
        if self.gravity_accumulator.len() < self.min_samples { return None; }
        let sum = self.gravity_accumulator.iter()
            .fold((0.0, 0.0, 0.0), |acc, &(x, y, z)| (acc.0 + x, acc.1 + y, acc.2 + z));
        let n = self.gravity_accumulator.len() as f64;
        let new = (sum.0 / n, sum.1 / n, sum.2 / n);
        self.gravity_estimate = (
            self.ema_alpha * new.0 + (1.0 - self.ema_alpha) * self.gravity_estimate.0,
            self.ema_alpha * new.1 + (1.0 - self.ema_alpha) * self.gravity_estimate.1,
            self.ema_alpha * new.2 + (1.0 - self.ema_alpha) * self.gravity_estimate.2,
        );
        self.refinement_count += 1;
        self.gravity_accumulator.clear();
        Some(self.gravity_estimate)
    }

    fn get_drift(&self) -> f64 {
        let d = (
            self.gravity_estimate.0 - self.gravity_startup.0,
            self.gravity_estimate.1 - self.gravity_startup.1,
            self.gravity_estimate.2 - self.gravity_startup.2,
        );
        (d.0 * d.0 + d.1 * d.1 + d.2 * d.2).sqrt()
    }

    fn drift_warning(&self) -> bool { self.get_drift() > self.drift_threshold }
}

// ─── The main fusion struct ──────────────────────────────────────────────────

pub struct SensorFusion {
    config: FusionConfig,

    // Primary filter
    pub ekf_15d: Ekf15d,

    // Shadow / comparison filters
    es_ekf: EsEkf,
    ekf_13d: Option<Ekf13d>,
    comp_filter: Option<ComplementaryFilter>,
    fgo: Option<GraphEstimator>,

    // Signal processing
    accel_lpf: LowPassFilter,
    accel_smoother: AccelSmoother,
    roughness_estimator: RoughnessEstimator,

    // Calibration
    gravity_bias: (f64, f64, f64),
    gyro_bias: (f64, f64, f64),
    calibration_complete: bool,
    dyn_calib: DynamicCalibration,

    // Incident detection
    incident_detector: IncidentDetector,
    incident_cooldown: IncidentCooldown,

    // GPS tracking
    last_gps_timestamp: f64,
    last_gps_fix_ts: Option<f64>,
    last_gps_speed: f64,
    recent_gps_speeds: VecDeque<(f64, f64)>,
    is_heading_initialized: bool,

    // Gap mode
    in_gap_mode: bool,

    // NHC / speed clamp timing
    last_nhc_ts: f64,
    last_speed_clamp_ts: f64,

    // ZUPT tracking
    last_accel_mag_raw: f64,
    last_gyro_mag: f64,

    // Timestamp validation
    last_accel_ts: Option<f64>,
    last_gyro_ts: Option<f64>,

    // Barometer (2-sample buffer for dP/dt)
    last_baro: Option<BaroData>,
    prev_baro: Option<BaroData>,

    // Cached state
    avg_roughness: f64,
    latest_mag: Option<MagData>,
    last_gyro_z: f64,
    last_gps_lat: Option<f64>,
    last_gps_lon: Option<f64>,
    kick_frames_remaining: u32,
}

impl SensorFusion {
    pub fn new(config: FusionConfig) -> Self {
        let gravity_bias = (0.0, 0.0, 9.81);

        let ekf_15d = Ekf15d::new(config.dt, config.gps_noise, config.accel_noise, config.gyro_noise);
        let es_ekf = EsEkf::new(config.dt, config.gps_noise, config.es_ekf_vel_noise, config.enable_gyro, config.gyro_noise);
        let ekf_13d = if config.enable_13d {
            Some(Ekf13d::new(config.dt, config.gps_noise, config.accel_noise, config.gyro_noise))
        } else { None };
        let comp_filter = if config.enable_complementary { Some(ComplementaryFilter::new()) } else { None };
        let fgo = if config.enable_fgo {
            Some(GraphEstimator::new((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)))
        } else { None };

        Self {
            accel_lpf: LowPassFilter::new(config.accel_lpf_cutoff_hz, config.accel_lpf_sample_hz),
            accel_smoother: AccelSmoother::new(config.accel_smoother_window),
            roughness_estimator: RoughnessEstimator::new(config.roughness_window_size, config.roughness_ewma_alpha),
            dyn_calib: DynamicCalibration::new(gravity_bias, &config),
            incident_detector: IncidentDetector::new(),
            incident_cooldown: IncidentCooldown::new(config.incident_cooldown_secs),
            ekf_15d, es_ekf, ekf_13d, comp_filter, fgo,
            gravity_bias, gyro_bias: (0.0, 0.0, 0.0), calibration_complete: false,
            last_gps_timestamp: 0.0, last_gps_fix_ts: None, last_gps_speed: 0.0,
            recent_gps_speeds: VecDeque::new(), is_heading_initialized: false,
            in_gap_mode: false, last_nhc_ts: -1.0, last_speed_clamp_ts: -1.0,
            last_accel_mag_raw: 0.0, last_gyro_mag: 0.0,
            last_accel_ts: None, last_gyro_ts: None,
            last_baro: None, prev_baro: None,
            avg_roughness: 0.0, latest_mag: None, last_gyro_z: 0.0,
            last_gps_lat: None, last_gps_lon: None, kick_frames_remaining: 0,
            config,
        }
    }

    // ── Calibration ──────────────────────────────────────────────────────

    pub fn set_calibration(&mut self, accel_samples: &VecDeque<AccelData>, gyro_samples: &VecDeque<GyroData>) -> bool {
        let (gravity, gyro) = calculate_biases(accel_samples, gyro_samples);
        self.gravity_bias = gravity;
        self.gyro_bias = gyro;
        self.dyn_calib = DynamicCalibration::new(gravity, &self.config);
        self.calibration_complete = accel_samples.len() >= 50;
        self.calibration_complete
    }

    pub fn set_biases(&mut self, gravity: (f64, f64, f64), gyro: (f64, f64, f64)) {
        self.gravity_bias = gravity;
        self.gyro_bias = gyro;
        self.dyn_calib = DynamicCalibration::new(gravity, &self.config);
        self.calibration_complete = true;
    }

    // ── Sensor feeds ─────────────────────────────────────────────────────

    /// Feed accelerometer sample (primary 50 Hz tick).
    pub fn feed_accel(&mut self, accel: &AccelData) -> Vec<FusionEvent> {
        let mut events = Vec::new();

        // Timestamp validation
        if let Some(prev_ts) = self.last_accel_ts {
            let dt = accel.timestamp - prev_ts;
            if dt <= 0.0 || dt > 1.0 { self.last_accel_ts = Some(accel.timestamp); return events; }
        }
        self.last_accel_ts = Some(accel.timestamp);

        // Low-pass filter
        let raw_vec = Vector3::new(accel.x, accel.y, accel.z);
        let filtered_vec = self.accel_lpf.update(raw_vec);
        self.last_accel_mag_raw = filtered_vec.norm();

        // Gravity subtraction
        let gravity_vec = Vector3::new(self.gravity_bias.0, self.gravity_bias.1, self.gravity_bias.2);
        let corrected_vec = filtered_vec - gravity_vec;
        let corrected_x = corrected_vec.x;
        let mut corrected_y = corrected_vec.y;
        let corrected_z = corrected_vec.z;

        // Roughness estimation
        self.avg_roughness = self.roughness_estimator.update(corrected_vec.x, corrected_vec.y, corrected_vec.z);

        // Virtual kick (testing)
        if self.kick_frames_remaining > 0 { corrected_y += 5.0; self.kick_frames_remaining -= 1; }

        let corrected_mag = (corrected_x * corrected_x + corrected_y * corrected_y + corrected_z * corrected_z).sqrt();
        let _smoothed_mag = self.accel_smoother.apply(corrected_mag);

        // GPS gap mode + speed clamping
        let gps_gap = self.gps_gap_at(accel.timestamp);
        events.extend(self.update_gap_mode(accel.timestamp, gps_gap));
        events.extend(self.enforce_speed_envelope(accel.timestamp, gps_gap));

        // 15D prediction (raw filtered accel — 15D handles its own bias internally)
        self.ekf_15d.predict((filtered_vec.x, filtered_vec.y, filtered_vec.z), (0.0, 0.0, 0.0));

        // 13D prediction (gravity-corrected accel)
        if let Some(ref mut ekf_13d) = self.ekf_13d {
            ekf_13d.predict((corrected_x, corrected_y, corrected_z), (0.0, 0.0, 0.0));
        }

        // Barometer vertical constraint (during GPS gaps)
        if self.config.enable_baro && gps_gap > self.config.mag_min_gps_gap {
            self.apply_baro_constraint();
        }

        // NHC lateral constraint
        events.extend(self.apply_nhc(accel.timestamp));

        // Magnetometer yaw assist (during GPS gaps)
        if self.config.enable_mag && gps_gap > self.config.mag_min_gps_gap {
            events.extend(self.apply_mag_yaw(gps_gap));
        }

        // Secondary filters (only when moving)
        let is_still = self.is_stationary();
        if !is_still {
            let _ = self.es_ekf.update_accelerometer_vector(corrected_x, corrected_y, corrected_z);
            if let Some(ref mut comp) = self.comp_filter {
                let _ = comp.update(corrected_x, corrected_y, corrected_z, 0.0, 0.0, 0.0);
            }
        }

        // Incident detection
        if self.incident_cooldown.ready_and_touch(accel.timestamp) {
            let shock_val = raw_vec.norm();
            let detection_val = if shock_val > self.config.crash_threshold { shock_val } else { corrected_mag };
            if let Some(incident) = self.incident_detector.detect(
                detection_val, self.last_gyro_z, None, accel.timestamp, self.last_gps_lat, self.last_gps_lon,
            ) {
                events.push(FusionEvent::IncidentDetected(incident));
            }
        }

        // FGO preintegrator
        if let Some(ref mut fgo) = self.fgo {
            fgo.enqueue_imu(Vector3::new(corrected_x, corrected_y, corrected_z), Vector3::zeros(), accel.timestamp);
        }

        // Stationary processing (gravity accumulation + 15D alignment)
        if is_still && self.avg_roughness < self.config.roughness_smooth_threshold {
            self.dyn_calib.accumulate(filtered_vec.x, filtered_vec.y, filtered_vec.z);
            self.ekf_15d.update_stationary_accel((filtered_vec.x, filtered_vec.y, filtered_vec.z));
        }

        events
    }

    /// Feed gyroscope sample.
    pub fn feed_gyro(&mut self, gyro: &GyroData) -> Vec<FusionEvent> {
        let events = Vec::new();

        // Timestamp validation
        if let Some(prev_ts) = self.last_gyro_ts {
            let dt = gyro.timestamp - prev_ts;
            if dt <= 0.0 || dt > 1.0 { self.last_gyro_ts = Some(gyro.timestamp); return events; }
        }
        self.last_gyro_ts = Some(gyro.timestamp);

        // Bias subtraction
        let corrected_gx = gyro.x - self.gyro_bias.0;
        let corrected_gy = gyro.y - self.gyro_bias.1;
        let mut corrected_gz = gyro.z - self.gyro_bias.2;

        // Straight-road yaw clamp
        if corrected_gz.abs() < self.config.gyro_straight_threshold && self.ekf_15d.get_speed() > self.config.gyro_straight_min_speed {
            corrected_gz = 0.0;
        }

        // Track gyro magnitude + cache z for incident detection
        self.last_gyro_mag = (corrected_gx * corrected_gx + corrected_gy * corrected_gy + corrected_gz * corrected_gz).sqrt();
        self.last_gyro_z = corrected_gz;

        // 15D gyro prediction
        self.ekf_15d.predict((0.0, 0.0, 0.0), (corrected_gx, corrected_gy, corrected_gz));

        // 13D gyro prediction
        if let Some(ref mut ekf_13d) = self.ekf_13d {
            ekf_13d.predict((0.0, 0.0, 0.0), (corrected_gx, corrected_gy, corrected_gz));
        }

        // Stationary gyro bias update (feed RAW gyro — 15D estimates its own bias)
        if self.last_accel_mag_raw > self.config.zupt_accel_low
            && self.last_accel_mag_raw < self.config.zupt_accel_high
            && self.last_gyro_mag < self.config.zupt_gyro_threshold
        {
            self.ekf_15d.update_stationary_gyro((gyro.x, gyro.y, gyro.z));
        }

        // FGO
        if let Some(ref mut fgo) = self.fgo {
            fgo.enqueue_imu(Vector3::zeros(), Vector3::new(corrected_gx, corrected_gy, corrected_gz), gyro.timestamp);
        }

        // EsEKF gyro (only when moving)
        if !self.is_stationary() {
            let _ = self.es_ekf.update_gyroscope(corrected_gx, corrected_gy, corrected_gz);
        }

        events
    }

    /// Feed GPS fix (~1 Hz measurement update).
    /// `system_time`: current wall-clock seconds. In replay mode, pass gps.timestamp.
    pub fn feed_gps(&mut self, gps: &GpsData, system_time: f64) -> Vec<FusionEvent> {
        let mut events = Vec::new();

        if gps.timestamp <= self.last_gps_timestamp { return events; }

        // Accuracy gating
        if gps.accuracy > self.config.gps_max_accuracy {
            events.push(FusionEvent::GpsRejected { accuracy: gps.accuracy, speed: gps.speed });
            return events;
        }
        self.last_gps_timestamp = gps.timestamp;

        // Latency compensation
        let latency = (system_time - gps.timestamp).max(0.0);
        if latency > self.config.gps_max_latency {
            events.push(FusionEvent::HighGpsLatency { latency_secs: latency });
        }

        let st = self.ekf_15d.get_state();
        let speed = (st.velocity.0 * st.velocity.0 + st.velocity.1 * st.velocity.1).sqrt();

        let (proj_lat, proj_lon) = if latency < self.config.gps_max_latency && speed < self.config.gps_max_projection_speed {
            (
                gps.latitude + (st.velocity.1 * latency) / 6371000.0 * 180.0 / std::f64::consts::PI,
                gps.longitude + (st.velocity.0 * latency) / (6371000.0 * (gps.latitude.to_radians().cos() + 1e-9)) * 180.0 / std::f64::consts::PI,
            )
        } else {
            (gps.latitude, gps.longitude)
        };

        // Cold start: first GPS fix initializes origin
        let is_first = self.ekf_13d.as_ref().map(|f| !f.is_origin_set()).unwrap_or(true);

        if is_first {
            if let Some(ref mut ekf_13d) = self.ekf_13d { ekf_13d.set_origin(gps.latitude, gps.longitude); }
            self.ekf_15d.set_origin(gps.latitude, gps.longitude, 0.0);
            self.ekf_15d.force_zero_velocity();
            events.push(FusionEvent::ColdStartInitialized { lat: gps.latitude, lon: gps.longitude });
        } else {
            // Normal GPS update
            self.ekf_15d.update_gps((proj_lat, proj_lon, 0.0), gps.accuracy);
            self.ekf_15d.update_gps_velocity(gps.speed, gps.bearing.to_radians(), self.config.gps_vel_std);
            if let Some(ref mut ekf_13d) = self.ekf_13d {
                ekf_13d.update_gps(proj_lat, proj_lon, proj_lat, proj_lon);
            }
        }

        // EsEKF update
        self.es_ekf.update_gps(proj_lat, proj_lon, Some(gps.speed), Some(gps.accuracy));

        // Heading alignment (first high-speed fix)
        if gps.speed > 5.0 && !self.is_heading_initialized {
            let gps_yaw = (90.0 - gps.bearing).to_radians();
            self.es_ekf.state_set_heading(gps_yaw);
            let half = gps_yaw * 0.5;
            self.ekf_15d.state[6] = half.cos();
            self.ekf_15d.state[7] = 0.0;
            self.ekf_15d.state[8] = 0.0;
            self.ekf_15d.state[9] = half.sin();
            self.is_heading_initialized = true;
            events.push(FusionEvent::HeadingAligned { bearing_deg: gps.bearing, yaw_deg: gps_yaw.to_degrees(), speed: gps.speed });
        }

        // Stationary forcing / vertical clamp (BUG FIX: removed duplicate update_gps_velocity)
        if gps.speed < self.config.gps_stationary_speed {
            self.ekf_15d.update_velocity((0.0, 0.0, 0.0), 1e-3);
        } else {
            self.ekf_15d.zero_vertical_velocity(1e-4);
        }

        // FGO
        if let Some(ref mut fgo) = self.fgo {
            fgo.add_gps_measurement(gps.latitude, gps.longitude, 0.0, gps.timestamp, gps.speed);
            let stats = fgo.get_stats();
            if stats.2 % 10 == 0 && stats.2 > 0 {
                events.push(FusionEvent::FgoOptimization { nodes: stats.0, gps_factors: stats.1, iteration: stats.2 });
            }
        }

        // Speed envelope bookkeeping
        self.recent_gps_speeds.push_back((gps.timestamp, gps.speed));
        while let Some((ts, _)) = self.recent_gps_speeds.front() {
            if gps.timestamp - *ts > self.config.gps_speed_window { self.recent_gps_speeds.pop_front(); }
            else { break; }
        }
        self.last_gps_fix_ts = Some(gps.timestamp);
        self.last_gps_speed = gps.speed;
        self.last_gps_lat = Some(gps.latitude);
        self.last_gps_lon = Some(gps.longitude);

        // Exit gap mode
        if self.in_gap_mode {
            self.in_gap_mode = false;
            events.push(FusionEvent::GapModeExited);
        }

        events
    }

    pub fn feed_mag(&mut self, mag: &MagData) { self.latest_mag = Some(mag.clone()); }

    pub fn feed_baro(&mut self, baro: &BaroData) {
        self.prev_baro = self.last_baro.take();
        self.last_baro = Some(baro.clone());
    }

    // ── Per-tick (call after feed_accel + feed_gyro each 50Hz cycle) ─────

    pub fn tick(&mut self) -> Vec<FusionEvent> {
        let mut events = Vec::new();

        if self.is_stationary() {
            self.es_ekf.apply_zupt();
            if let Some(ref mut comp) = self.comp_filter { comp.apply_zupt(); }
            self.ekf_15d.force_zero_velocity();
            events.push(FusionEvent::ZuptApplied);

            // Dynamic gravity refinement
            if let Some(estimate) = self.dyn_calib.try_refine() {
                self.gravity_bias = estimate;
                let mag = (estimate.0 * estimate.0 + estimate.1 * estimate.1 + estimate.2 * estimate.2).sqrt();
                events.push(FusionEvent::GravityRefined {
                    refinement_count: self.dyn_calib.refinement_count, estimate, magnitude: mag, drift: self.dyn_calib.get_drift(),
                });
                if self.dyn_calib.drift_warning() {
                    events.push(FusionEvent::GravityDriftWarning { drift: self.dyn_calib.get_drift(), threshold: self.dyn_calib.drift_threshold });
                }
            }
        }

        let _ = self.es_ekf.predict();
        events
    }

    // ── Queries ──────────────────────────────────────────────────────────

    pub fn get_snapshot(&self) -> FusionSnapshot {
        FusionSnapshot {
            ekf_15d_state: self.ekf_15d.get_state(),
            ekf_13d_state: self.ekf_13d.as_ref().map(|f| f.get_state()),
            es_ekf_state: self.es_ekf.get_state(),
            comp_state: self.comp_filter.as_ref().and_then(|f| f.get_state()),
            fgo_state: self.fgo.as_ref().map(|f| f.get_current_state()),
            gravity_bias: self.gravity_bias,
            gyro_bias: self.gyro_bias,
            calibration_complete: self.calibration_complete,
            gravity_refinements: self.dyn_calib.refinement_count,
            gravity_drift: self.dyn_calib.get_drift(),
            roughness: self.avg_roughness,
            is_stationary: self.is_stationary(),
            in_gap_mode: self.in_gap_mode,
            gps_gap_secs: self.last_accel_ts.map(|t| self.gps_gap_at(t)).unwrap_or(0.0),
            heading_initialized: self.is_heading_initialized,
        }
    }

    pub fn is_stationary(&self) -> bool {
        self.last_accel_mag_raw > self.config.zupt_accel_low
            && self.last_accel_mag_raw < self.config.zupt_accel_high
            && self.last_gyro_mag < self.config.zupt_gyro_threshold
    }

    pub fn get_speed(&self) -> f64 { self.ekf_15d.get_speed() }

    pub fn get_covariance_snapshot(&self) -> (f64, [f64; 8]) {
        self.es_ekf.get_covariance_snapshot()
    }

    pub fn trigger_kick(&mut self, frames: u32) { self.kick_frames_remaining = frames; }

    pub fn config(&self) -> &FusionConfig { &self.config }

    // ── Internal helpers ─────────────────────────────────────────────────

    fn gps_gap_at(&self, timestamp: f64) -> f64 {
        self.last_gps_fix_ts.map(|ts| (timestamp - ts).max(0.0)).unwrap_or(f64::INFINITY)
    }

    fn update_gap_mode(&mut self, _timestamp: f64, gap: f64) -> Vec<FusionEvent> {
        let mut events = Vec::new();
        if self.last_gps_fix_ts.is_none() { self.in_gap_mode = false; return events; }

        if gap > self.config.gap_clamp_trigger || (self.in_gap_mode && gap > self.config.gap_clamp_hyst) {
            self.in_gap_mode = true;
            let limit = if self.last_gps_speed < 1.0 { 2.0 }
                else if self.last_gps_speed < 5.0 { self.last_gps_speed * 2.0 + self.config.gap_clamp_offset }
                else { self.config.gap_clamp_scale * self.last_gps_speed + self.config.gap_clamp_offset }
                .max(2.0);
            let ekf_speed = self.ekf_15d.get_speed();
            if ekf_speed > limit {
                self.ekf_15d.clamp_speed(limit);
                events.push(FusionEvent::GapClampActive { gap_secs: gap, speed: ekf_speed, limit });
            }
        }
        events
    }

    fn enforce_speed_envelope(&mut self, timestamp: f64, gap: f64) -> Vec<FusionEvent> {
        let mut events = Vec::new();
        let max_recent = self.recent_gps_speeds.iter().map(|(_, s)| *s).fold(0.0_f64, f64::max);
        if max_recent <= 3.0 { return events; }

        let ekf_speed = self.ekf_15d.get_speed();
        let (scale, offset) = if gap > 5.0 { (self.config.gap_clamp_scale, self.config.gap_clamp_offset) }
            else { (self.config.normal_clamp_scale, self.config.normal_clamp_offset) };
        let limit = scale * max_recent + offset;
        if ekf_speed > limit && ekf_speed > 1e-3 {
            self.ekf_15d.clamp_speed(limit);
            self.last_speed_clamp_ts = timestamp;
            events.push(FusionEvent::SpeedClamped { from_speed: ekf_speed, to_limit: limit, gap_secs: gap });
        }
        events
    }

    fn apply_nhc(&mut self, timestamp: f64) -> Vec<FusionEvent> {
        let mut events = Vec::new();
        if self.last_nhc_ts >= 0.0 && (timestamp - self.last_nhc_ts) < self.config.nhc_interval_secs { return events; }

        let nhc_gap = self.gps_gap_at(timestamp);
        if nhc_gap <= self.config.nhc_max_gap_secs {
            let nhc_r = (1.0 + nhc_gap * 0.5).min(5.0);
            self.ekf_15d.update_body_velocity(Vector3::zeros(), nhc_r);
        } else {
            events.push(FusionEvent::NhcSkipped { gap_secs: nhc_gap });
        }
        self.last_nhc_ts = timestamp;
        events
    }

    fn apply_mag_yaw(&mut self, gps_gap: f64) -> Vec<FusionEvent> {
        let mut events = Vec::new();
        if self.last_gps_speed <= self.config.mag_min_speed || self.ekf_15d.get_speed() <= self.config.mag_min_speed {
            return events;
        }
        if let Some(ref mag) = self.latest_mag {
            if let Some(innov) = self.ekf_15d.update_mag_heading(mag, self.config.mag_declination_rad) {
                events.push(FusionEvent::MagCorrection { gap_secs: gps_gap, innovation_deg: innov.to_degrees() });
            }
        }
        events
    }

    fn apply_baro_constraint(&mut self) {
        if let (Some(ref curr), Some(ref prev)) = (&self.last_baro, &self.prev_baro) {
            let dt = (curr.timestamp - prev.timestamp).max(1e-3);
            let dp_dt_pa = ((curr.pressure_hpa - prev.pressure_hpa) / dt) * 100.0;
            let stable = dp_dt_pa.abs() < self.config.baro_pressure_rate_threshold;
            if self.last_gps_speed > self.config.baro_min_speed {
                let noise_var = if stable { 5e-3 } else { 1e-1 };
                self.ekf_15d.zero_vertical_velocity(noise_var);
            }
        }
    }
}

// ─── Utility ─────────────────────────────────────────────────────────────────

pub fn calculate_biases(
    accel_samples: &VecDeque<AccelData>,
    gyro_samples: &VecDeque<GyroData>,
) -> ((f64, f64, f64), (f64, f64, f64)) {
    let accel_count = accel_samples.len();
    let mut asum = (0.0, 0.0, 0.0);
    for s in accel_samples { asum.0 += s.x; asum.1 += s.y; asum.2 += s.z; }
    let gravity = if accel_count > 0 {
        (asum.0 / accel_count as f64, asum.1 / accel_count as f64, asum.2 / accel_count as f64)
    } else { (0.0, 0.0, 9.81) };

    let gyro_count = gyro_samples.len();
    let mut gsum = (0.0, 0.0, 0.0);
    for s in gyro_samples { gsum.0 += s.x; gsum.1 += s.y; gsum.2 += s.z; }
    let gyro = if gyro_count > 0 {
        (gsum.0 / gyro_count as f64, gsum.1 / gyro_count as f64, gsum.2 / gyro_count as f64)
    } else { (0.0, 0.0, 0.0) };

    (gravity, gyro)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_zupt_detection() {
        let mut fusion = SensorFusion::new(FusionConfig::default());
        fusion.set_biases((0.0, 0.0, 9.81), (0.0, 0.0, 0.0));

        let accel = AccelData { timestamp: 1.0, x: 0.0, y: 0.0, z: 9.81 };
        fusion.feed_accel(&accel);
        let gyro = GyroData { timestamp: 1.0, x: 0.0, y: 0.0, z: 0.0 };
        fusion.feed_gyro(&gyro);

        assert!(fusion.is_stationary());
    }

    #[test]
    fn test_gps_cold_start() {
        let mut fusion = SensorFusion::new(FusionConfig::default());
        fusion.set_biases((0.0, 0.0, 9.81), (0.0, 0.0, 0.0));

        let gps = GpsData {
            timestamp: 1.0, latitude: 32.2, longitude: -110.9,
            speed: 0.0, bearing: 0.0, accuracy: 5.0,
        };
        let events = fusion.feed_gps(&gps, 1.0);

        assert!(events.iter().any(|e| matches!(e, FusionEvent::ColdStartInitialized { .. })));
    }

    #[test]
    fn test_gap_mode_activates() {
        let mut fusion = SensorFusion::new(FusionConfig::default());
        fusion.set_biases((0.0, 0.0, 9.81), (0.0, 0.0, 0.0));

        let gps = GpsData { timestamp: 1.0, latitude: 32.2, longitude: -110.9,
            speed: 20.0, bearing: 90.0, accuracy: 5.0 };
        fusion.feed_gps(&gps, 1.0);

        let accel = AccelData { timestamp: 7.0, x: 0.0, y: 2.0, z: 9.81 };
        fusion.feed_accel(&accel);

        let snapshot = fusion.get_snapshot();
        assert!(snapshot.in_gap_mode);
    }
}
