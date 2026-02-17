//! SensorFusion — pure-computation fusion layer wrapping Ekf15d.
//!
//! No tokio, no async, no I/O, no Termux, no Rerun.
//! Owns the EKF plus all ancillary logic (ZUPT, NHC, gap clamping,
//! dynamic gravity calibration, heading init, etc.).

use std::collections::VecDeque;

use nalgebra::Vector3;

use crate::filters::ekf_15d::Ekf15d;
use crate::types::{AccelData, BaroData, GpsData, GyroData, MagData};

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
pub struct FusionConfig {
    pub dt: f64,
    pub gps_noise: f64,
    pub accel_noise: f64,
    pub gyro_noise: f64,
    pub q_vel: f64,
    pub gps_vel_std: f64,
    pub normal_clamp_scale: f64,
    pub normal_clamp_offset: f64,
    pub gap_clamp_scale: f64,
    pub gap_clamp_offset: f64,
    pub gap_clamp_trigger: f64,
    pub accel_lpf_cutoff_hz: f64,
    pub accel_lpf_sample_hz: f64,
    pub zupt_accel_low: f64,
    pub zupt_accel_high: f64,
    pub zupt_gyro_threshold: f64,
    pub nhc_interval_secs: f64,
    pub nhc_max_gap_secs: f64,
    pub nhc_r: f64,
    pub gps_max_accuracy: f64,
    pub gps_stationary_speed: f64,
    pub heading_init_speed: f64,
    pub yaw_correct_blend: f64,
    pub gyro_straight_threshold: f64,
    pub gyro_straight_min_speed: f64,
    pub mag_declination_rad: f64,
    pub enable_mag: bool,
    pub enable_baro: bool,
}

impl Default for FusionConfig {
    fn default() -> Self {
        Self {
            dt: 0.02,
            gps_noise: 8.0,
            accel_noise: 3.0,
            gyro_noise: 0.0005,
            q_vel: 0.5,
            gps_vel_std: 0.3,
            normal_clamp_scale: 1.5,
            normal_clamp_offset: 5.0,
            gap_clamp_scale: 1.1,
            gap_clamp_offset: 2.0,
            gap_clamp_trigger: 5.0,
            accel_lpf_cutoff_hz: 4.0,
            accel_lpf_sample_hz: 50.0,
            zupt_accel_low: 9.5,
            zupt_accel_high: 10.1,
            zupt_gyro_threshold: 0.1,
            nhc_interval_secs: 1.0,
            nhc_max_gap_secs: 10.0,
            nhc_r: 0.1,
            gps_max_accuracy: 50.0,
            gps_stationary_speed: 0.5,
            heading_init_speed: 5.0,
            yaw_correct_blend: 0.7,
            gyro_straight_threshold: 0.02,
            gyro_straight_min_speed: 5.0,
            mag_declination_rad: 0.157,
            enable_mag: false,
            enable_baro: false,
        }
    }
}

// ---------------------------------------------------------------------------
// Events
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
pub enum FusionEvent {
    SpeedClamped { from_speed: f64, to_limit: f64, gap_secs: f64 },
    GpsRejected { accuracy: f64, speed: f64 },
    ColdStartInitialized { lat: f64, lon: f64 },
    HeadingAligned { bearing_deg: f64, speed: f64 },
    NhcApplied { gap_secs: f64, r: f64 },
    GravityRefined { refinement_count: u64, estimate: (f64, f64, f64), drift: f64 },
    ZuptApplied,
    GapModeExited,
}

// ---------------------------------------------------------------------------
// Snapshot
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
pub struct FusionSnapshot {
    pub position: (f64, f64, f64),
    pub velocity: (f64, f64, f64),
    pub speed: f64,
    pub quaternion: (f64, f64, f64, f64),
    pub heading_deg: f64,
    pub gravity_bias: (f64, f64, f64),
    pub gyro_bias: (f64, f64, f64),
    pub is_stationary: bool,
    pub in_gap_mode: bool,
    pub gps_gap_secs: f64,
    pub heading_initialized: bool,
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

struct LowPassFilter {
    alpha: f64,
    last: Vector3<f64>,
    initialized: bool,
}

impl LowPassFilter {
    fn new(cutoff_hz: f64, sample_hz: f64) -> Self {
        let dt = 1.0 / sample_hz;
        let rc = 1.0 / (2.0 * std::f64::consts::PI * cutoff_hz);
        Self {
            alpha: dt / (rc + dt),
            last: Vector3::zeros(),
            initialized: false,
        }
    }

    fn update(&mut self, v: Vector3<f64>) -> Vector3<f64> {
        if !self.initialized {
            self.last = v;
            self.initialized = true;
            return v;
        }
        self.last = self.last * (1.0 - self.alpha) + v * self.alpha;
        self.last
    }
}

struct DynamicCalibration {
    accumulator: Vec<(f64, f64, f64)>,
    estimate: (f64, f64, f64),
    startup: (f64, f64, f64),
    refinement_count: u64,
    ema_alpha: f64,
    min_samples: usize,
}

impl DynamicCalibration {
    fn new(initial: (f64, f64, f64)) -> Self {
        Self {
            accumulator: Vec::with_capacity(100),
            estimate: initial,
            startup: initial,
            refinement_count: 0,
            ema_alpha: 0.1,
            min_samples: 30,
        }
    }

    fn accumulate(&mut self, ax: f64, ay: f64, az: f64) {
        self.accumulator.push((ax, ay, az));
    }

    /// Try to refine gravity estimate. Returns Some(event) if refinement happened.
    fn try_refine(&mut self) -> Option<FusionEvent> {
        if self.accumulator.len() < self.min_samples {
            return None;
        }
        let n = self.accumulator.len() as f64;
        let sum = self.accumulator.iter()
            .fold((0.0, 0.0, 0.0), |a, &(x, y, z)| (a.0 + x, a.1 + y, a.2 + z));
        let mean = (sum.0 / n, sum.1 / n, sum.2 / n);

        let a = self.ema_alpha;
        self.estimate = (
            a * mean.0 + (1.0 - a) * self.estimate.0,
            a * mean.1 + (1.0 - a) * self.estimate.1,
            a * mean.2 + (1.0 - a) * self.estimate.2,
        );
        self.refinement_count += 1;
        self.accumulator.clear();

        let drift = self.drift_magnitude();
        Some(FusionEvent::GravityRefined {
            refinement_count: self.refinement_count,
            estimate: self.estimate,
            drift,
        })
    }

    fn drift_magnitude(&self) -> f64 {
        let dx = self.estimate.0 - self.startup.0;
        let dy = self.estimate.1 - self.startup.1;
        let dz = self.estimate.2 - self.startup.2;
        (dx * dx + dy * dy + dz * dz).sqrt()
    }
}

// ---------------------------------------------------------------------------
// SensorFusion
// ---------------------------------------------------------------------------

pub struct SensorFusion {
    pub ekf: Ekf15d,
    config: FusionConfig,

    // Filters
    accel_lpf: LowPassFilter,

    // Calibration
    gravity_bias: (f64, f64, f64),
    gyro_bias: (f64, f64, f64),
    dyn_calib: DynamicCalibration,

    // GPS / gap state
    last_gps_fix_ts: Option<f64>,
    last_gps_speed: f64,
    in_gap_mode: bool,
    recent_gps_speeds: VecDeque<(f64, f64)>,
    heading_initialized: bool,

    // NHC
    last_nhc_ts: f64,

    // ZUPT
    last_accel_mag_filtered: f64,
    last_gyro_mag: f64,

    // Stored filtered accel for combined predict (accel+gyro in one call)
    last_filtered_accel: (f64, f64, f64),

    // Timestamps
    last_accel_ts: Option<f64>,
    last_gyro_ts: Option<f64>,
    last_gps_ts: Option<f64>,

    // Baro
    last_baro: Option<(f64, f64)>, // (timestamp, pressure_hpa)

    // Origin set?
    origin_set: bool,

    // Debug counters
    pub predict_count: u64,
    pub zupt_count: u64,
}

impl SensorFusion {
    pub fn new(config: FusionConfig) -> Self {
        let mut ekf = Ekf15d::new(config.dt, config.gps_noise, config.accel_noise, config.gyro_noise);
        for i in 3..6 {
            ekf.process_noise[(i, i)] = config.q_vel;
        }
        let accel_lpf = LowPassFilter::new(config.accel_lpf_cutoff_hz, config.accel_lpf_sample_hz);
        let dyn_calib = DynamicCalibration::new((0.0, 0.0, 9.81));
        Self {
            ekf,
            config,
            accel_lpf,
            gravity_bias: (0.0, 0.0, 9.81),
            gyro_bias: (0.0, 0.0, 0.0),
            dyn_calib,
            last_gps_fix_ts: None,
            last_gps_speed: 0.0,
            in_gap_mode: false,
            recent_gps_speeds: VecDeque::new(),
            heading_initialized: false,
            last_nhc_ts: -1.0,
            last_accel_mag_filtered: 0.0,
            last_gyro_mag: 0.0,
            last_filtered_accel: (0.0, 0.0, 9.81),
            last_accel_ts: None,
            last_gyro_ts: None,
            last_gps_ts: None,
            last_baro: None,
            origin_set: false,
            predict_count: 0,
            zupt_count: 0,
        }
    }

    /// Set calibration biases from stationary samples (like the live pipeline's
    /// 3-second preamble). Returns true if enough samples were present.
    pub fn set_calibration(
        &mut self,
        accel_samples: &VecDeque<AccelData>,
        gyro_samples: &VecDeque<GyroData>,
    ) -> bool {
        if accel_samples.len() < 10 {
            return false;
        }
        let n = accel_samples.len() as f64;
        let (mut sx, mut sy, mut sz) = (0.0, 0.0, 0.0);
        for a in accel_samples {
            sx += a.x;
            sy += a.y;
            sz += a.z;
        }
        self.gravity_bias = (sx / n, sy / n, sz / n);
        self.dyn_calib = DynamicCalibration::new(self.gravity_bias);

        if gyro_samples.len() >= 10 {
            let gn = gyro_samples.len() as f64;
            let (mut gx, mut gy, mut gz) = (0.0, 0.0, 0.0);
            for g in gyro_samples {
                gx += g.x;
                gy += g.y;
                gz += g.z;
            }
            self.gyro_bias = (gx / gn, gy / gn, gz / gn);
        }
        true
    }

    /// Directly set biases (e.g. from serialised session data).
    pub fn set_biases(&mut self, gravity: (f64, f64, f64), gyro: (f64, f64, f64)) {
        self.gravity_bias = gravity;
        self.gyro_bias = gyro;
        self.dyn_calib = DynamicCalibration::new(gravity);
    }

    // -----------------------------------------------------------------------
    // Sensor feeds
    // -----------------------------------------------------------------------

    pub fn feed_accel(&mut self, accel: &AccelData) -> Vec<FusionEvent> {
        let mut events = Vec::new();

        // 1. Skip bad timestamp delta
        if let Some(prev) = self.last_accel_ts {
            let dt = accel.timestamp - prev;
            if dt <= 0.0 || dt > 1.0 {
                self.last_accel_ts = Some(accel.timestamp);
                return events;
            }
            self.ekf.dt = dt;
        }
        self.last_accel_ts = Some(accel.timestamp);

        // 2. Low-pass filter
        let raw = Vector3::new(accel.x, accel.y, accel.z);
        let filtered = self.accel_lpf.update(raw);

        // 3. Store filtered magnitude for ZUPT
        self.last_accel_mag_filtered = filtered.norm();

        // 4. (gravity subtraction is for legacy filters; 15D EKF handles
        //     gravity internally — we feed raw/filtered to predict)

        // 5. Gap mode detection (clamping happens post-predict in feed_gyro)
        let gap = self.gps_gap_at(accel.timestamp);
        if gap > self.config.gap_clamp_trigger || (self.in_gap_mode && gap > 0.5) {
            self.in_gap_mode = true;
        }

        // 6. Store filtered accel for combined predict in feed_gyro.
        //    CRITICAL: Do NOT call predict here — splitting accel and gyro into
        //    separate predict() calls breaks quaternion integration and causes
        //    massive drift. The actual predict(accel, gyro) happens in feed_gyro.
        self.last_filtered_accel = (filtered.x, filtered.y, filtered.z);

        // NHC and stationary updates happen in feed_gyro, after the combined predict.

        events
    }

    pub fn feed_gyro(&mut self, gyro: &GyroData) -> Vec<FusionEvent> {
        let mut events = Vec::new();

        // 1. Skip bad timestamp delta
        if let Some(prev) = self.last_gyro_ts {
            let dt = gyro.timestamp - prev;
            if dt <= 0.0 || dt > 1.0 {
                self.last_gyro_ts = Some(gyro.timestamp);
                return events;
            }
            self.ekf.dt = dt;
        }
        self.last_gyro_ts = Some(gyro.timestamp);

        // 2. Compute bias-corrected gyro for ZUPT / threshold checks only.
        //    Do NOT feed corrected gyro to predict — the 15D EKF already
        //    subtracts its own internal gyro bias (state[10..12]).
        //    External subtraction would double-correct.
        let cgx = gyro.x - self.gyro_bias.0;
        let cgy = gyro.y - self.gyro_bias.1;
        let cgz = gyro.z - self.gyro_bias.2;

        // 3. Store corrected gyro magnitude for ZUPT detection
        self.last_gyro_mag = (cgx * cgx + cgy * cgy + cgz * cgz).sqrt();

        // 4. Straight-road yaw clamp: if corrected gz is near zero on a
        //    straight road, set RAW gz to the EKF's current bias estimate
        //    so that the EKF sees an effective zero rotation.
        let mut gz_for_predict = gyro.z;
        let spd = self.ekf.get_speed();
        if cgz.abs() < self.config.gyro_straight_threshold
            && spd > self.config.gyro_straight_min_speed
        {
            // Setting raw to the EKF's internal bias makes (raw - bias) = 0 inside motion_model
            gz_for_predict = self.ekf.state[12]; // state[12] = gyro_bias_z
        }

        // 5. COMBINED predict with stored filtered accel + RAW gyro.
        //    CRITICAL: Accel and gyro MUST be in a single predict() call.
        //    Splitting them breaks quaternion rotation matrix integration.
        //    The EKF subtracts accel_bias and gyro_bias internally.
        self.predict_count += 1;
        self.ekf.predict(self.last_filtered_accel, (gyro.x, gyro.y, gz_for_predict));

        // 5b. Speed ceiling AFTER predict (prevents velocity divergence during GPS gaps).
        //     Must be post-predict: predict adds velocity from gravity subtraction errors,
        //     clamp catches it immediately instead of letting it grow.
        let gap = self.gps_gap_at(gyro.timestamp);
        let cfg = &self.config;
        if self.in_gap_mode {
            let limit = if self.last_gps_speed < 1.0 {
                2.0
            } else if self.last_gps_speed < 5.0 {
                self.last_gps_speed * 2.0 + cfg.gap_clamp_offset
            } else {
                cfg.gap_clamp_scale * self.last_gps_speed + cfg.gap_clamp_offset
            }
            .max(2.0);
            let spd = self.ekf.get_speed();
            if spd > limit {
                self.ekf.clamp_speed(limit);
            }
        }
        // Speed envelope clamp (post-predict)
        if let Some(max_recent) = self.recent_gps_speeds.iter().map(|(_, s)| *s)
            .max_by(|a, b| a.partial_cmp(b).unwrap())
        {
            if max_recent > 3.0 {
                let spd = self.ekf.get_speed();
                let (scale, offset) = if gap > 5.0 {
                    (cfg.gap_clamp_scale, cfg.gap_clamp_offset)
                } else {
                    (cfg.normal_clamp_scale, cfg.normal_clamp_offset)
                };
                let limit = scale * max_recent + offset;
                if spd > limit && spd > 1e-3 {
                    self.ekf.clamp_speed(limit);
                }
            }
        }

        // 6. NHC: constrain lateral/vertical body velocity (post-predict)
        let gap = self.gps_gap_at(gyro.timestamp);
        let cfg = &self.config;
        if (self.last_nhc_ts < 0.0 || (gyro.timestamp - self.last_nhc_ts) >= cfg.nhc_interval_secs)
            && gap < cfg.nhc_max_gap_secs
        {
            let spd = self.ekf.get_speed();
            if gap < 3.0 && spd > 2.5 {
                let r = (cfg.nhc_r + gap * 0.5).min(5.0);
                self.ekf.update_body_velocity_with_offset(spd, 0.0, r);
                events.push(FusionEvent::NhcApplied { gap_secs: gap, r });
            }
            self.last_nhc_ts = gyro.timestamp;
        }

        // 7. Stationary gyro bias update (ZUPT) — use RAW gyro
        if self.is_stationary() {
            self.ekf.update_stationary_gyro((gyro.x, gyro.y, gyro.z));
            // Also accumulate filtered accel for dynamic gravity refinement
            let fa = self.last_filtered_accel;
            self.dyn_calib.accumulate(fa.0, fa.1, fa.2);
            self.ekf.update_stationary_accel(fa);
        }

        events
    }

    pub fn feed_gps(&mut self, gps: &GpsData, _system_time: f64) -> Vec<FusionEvent> {
        let mut events = Vec::new();

        // 1. Skip duplicate / out-of-order
        if let Some(prev) = self.last_gps_ts {
            if gps.timestamp <= prev {
                return events;
            }
        }
        self.last_gps_ts = Some(gps.timestamp);

        // 2. Reject bad accuracy
        if gps.accuracy > self.config.gps_max_accuracy {
            events.push(FusionEvent::GpsRejected {
                accuracy: gps.accuracy,
                speed: gps.speed,
            });
            return events;
        }

        // 3. First fix → cold start init
        if !self.origin_set {
            self.ekf.set_origin(gps.latitude, gps.longitude, 0.0);
            self.ekf.initialize_from_gps(gps.latitude, gps.longitude, 0.0, gps.accuracy);
            self.ekf.force_zero_velocity();

            // Initialize roll/pitch from gravity vector so gravity subtraction works.
            // Without this, the identity quaternion assumes the phone is flat, and
            // gravity appears as horizontal acceleration when the phone is tilted.
            let (gx, gy, gz) = self.gravity_bias;
            let g_mag = (gx * gx + gy * gy + gz * gz).sqrt();
            if g_mag > 1.0 {
                let roll = gy.atan2(gz);
                let pitch = (-gx).atan2((gy * gy + gz * gz).sqrt());
                let q = nalgebra::UnitQuaternion::from_euler_angles(roll, pitch, 0.0);
                self.ekf.state[6] = q.w;
                self.ekf.state[7] = q.i;
                self.ekf.state[8] = q.j;
                self.ekf.state[9] = q.k;
            }

            // Restore covariance to Ekf15d::new() defaults.
            // initialize_from_gps sets very tight covariance (vel=0.01, quat=0.1)
            // which makes the EKF barely trust the first GPS updates.
            for i in 0..3 { self.ekf.covariance[(i, i)] = 100.0; }
            for i in 3..6 { self.ekf.covariance[(i, i)] = 10.0; }
            for i in 6..10 { self.ekf.covariance[(i, i)] = 1.0; }
            for i in 10..13 { self.ekf.covariance[(i, i)] = 0.1; }
            for i in 13..15 { self.ekf.covariance[(i, i)] = 0.1; }
            self.origin_set = true;
            events.push(FusionEvent::ColdStartInitialized {
                lat: gps.latitude,
                lon: gps.longitude,
            });
            // Still continue — set gap tracking, heading, etc.
        } else {
            // 4. Normal GPS update (position + velocity, ONCE)
            let _nis = self.ekf.update_gps(
                (gps.latitude, gps.longitude, 0.0),
                gps.accuracy,
                gps.timestamp,
            );
            self.ekf.update_gps_velocity(
                gps.speed,
                gps.bearing.to_radians(),
                self.config.gps_vel_std,
            );
        }

        // 4b. Yaw correction from GPS velocity (preserves roll/pitch from IMU).
        //     Using blend=1.0 for fast convergence (full correction each fix).
        //     Must preserve roll/pitch to avoid gravity-as-horizontal-accel when
        //     phone is tilted (e.g., car mount at 30°).
        if gps.speed > self.config.heading_init_speed {
            self.ekf.correct_yaw_from_gps_velocity(
                gps.speed,
                gps.bearing.to_radians(),
                self.config.heading_init_speed,
                1.0, // full correction — converges in one fix
            );
        }

        // 5. Track heading initialization (the aggressive yaw override above handles
        //    alignment on every GPS fix with speed > threshold).
        if gps.speed > self.config.heading_init_speed && !self.heading_initialized {
            self.heading_initialized = true;
            events.push(FusionEvent::HeadingAligned {
                bearing_deg: gps.bearing,
                speed: gps.speed,
            });
        }

        // 6. Velocity constraints — only zero vertical for land vehicles
        self.ekf.zero_vertical_velocity(1e-4);

        // 7. Update gap tracking
        self.last_gps_fix_ts = Some(gps.timestamp);
        self.last_gps_speed = gps.speed;

        // Prune old speeds, push new
        while let Some((ts, _)) = self.recent_gps_speeds.front() {
            if gps.timestamp - *ts > 10.0 {
                self.recent_gps_speeds.pop_front();
            } else {
                break;
            }
        }
        self.recent_gps_speeds.push_back((gps.timestamp, gps.speed));

        // Exit gap mode on GPS arrival
        if self.in_gap_mode {
            self.in_gap_mode = false;
            events.push(FusionEvent::GapModeExited);
        }

        events
    }

    pub fn feed_mag(&mut self, mag: &MagData) {
        if !self.config.enable_mag {
            return;
        }
        let gap = self.gps_gap_at(mag.timestamp);
        if gap > 3.0 && self.last_gps_speed > 2.0 && self.ekf.get_speed() > 2.0 {
            let mag_data = crate::types::MagData {
                timestamp: mag.timestamp,
                x: mag.x,
                y: mag.y,
                z: mag.z,
            };
            let _correction = self.ekf.update_mag_heading(&mag_data, self.config.mag_declination_rad);
        }
    }

    pub fn feed_baro(&mut self, baro: &BaroData) {
        if !self.config.enable_baro {
            return;
        }
        let gap = self.gps_gap_at(baro.timestamp);
        if gap > 3.0 {
            if let Some((prev_ts, prev_hpa)) = self.last_baro {
                let dt = (baro.timestamp - prev_ts).max(1e-3);
                let dp_dt_hpa = (baro.pressure_hpa - prev_hpa) / dt;
                let dp_dt_pa = dp_dt_hpa * 100.0;
                let pressure_stable = dp_dt_pa.abs() < 0.5;
                if self.last_gps_speed > 1.0 {
                    let noise_var = if pressure_stable { 5e-3 } else { 1e-1 };
                    self.ekf.zero_vertical_velocity(noise_var);
                }
            }
        }
        self.last_baro = Some((baro.timestamp, baro.pressure_hpa));
    }

    /// Per-tick housekeeping (call once per accel sample).
    pub fn tick(&mut self) -> Vec<FusionEvent> {
        let mut events = Vec::new();
        // ZUPT: only force zero velocity if IMU says stationary AND the EKF
        // speed is already low. Without the speed check, ZUPT false-fires
        // on straight highways at constant speed (accel≈9.81, gyro≈0) and
        // zeros velocity mid-drive.
        if self.is_stationary() && self.ekf.get_speed() < 1.0 {
            self.zupt_count += 1;
            self.ekf.force_zero_velocity();
            let accel_vec = nalgebra::Vector3::new(
                self.last_filtered_accel.0,
                self.last_filtered_accel.1,
                self.last_filtered_accel.2,
            );
            self.ekf.apply_zupt(&accel_vec);
            events.push(FusionEvent::ZuptApplied);

            // Try gravity refinement
            if let Some(ev) = self.dyn_calib.try_refine() {
                self.gravity_bias = self.dyn_calib.estimate;
                events.push(ev);
            }
        }
        events
    }

    // -----------------------------------------------------------------------
    // Queries
    // -----------------------------------------------------------------------

    pub fn get_snapshot(&self) -> FusionSnapshot {
        let st = self.ekf.get_state();
        FusionSnapshot {
            position: st.position,
            velocity: st.velocity,
            speed: self.ekf.get_speed(),
            quaternion: st.quaternion,
            heading_deg: self.ekf.get_heading().to_degrees(),
            gravity_bias: self.gravity_bias,
            gyro_bias: self.ekf.get_gyro_bias(),
            is_stationary: self.is_stationary(),
            in_gap_mode: self.in_gap_mode,
            gps_gap_secs: self.last_gps_fix_ts
                .and_then(|ts| self.last_accel_ts.map(|a| (a - ts).max(0.0)))
                .unwrap_or(f64::INFINITY),
            heading_initialized: self.heading_initialized,
        }
    }

    pub fn is_stationary(&self) -> bool {
        self.last_accel_mag_filtered > self.config.zupt_accel_low
            && self.last_accel_mag_filtered < self.config.zupt_accel_high
            && self.last_gyro_mag < self.config.zupt_gyro_threshold
    }

    pub fn get_speed(&self) -> f64 {
        self.ekf.get_speed()
    }

    // -----------------------------------------------------------------------
    // Internal
    // -----------------------------------------------------------------------

    fn gps_gap_at(&self, now: f64) -> f64 {
        self.last_gps_fix_ts
            .map(|ts| (now - ts).max(0.0))
            .unwrap_or(f64::INFINITY)
    }
}
