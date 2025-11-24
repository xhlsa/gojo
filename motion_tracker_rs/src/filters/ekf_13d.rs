#![allow(dead_code)]

/// 13-Dimensional Extended Kalman Filter (Experimental Shadow Mode)
///
/// State Vector (13D):
/// [0-2]:   Position (X, Y, Z) in ECEF or local frame (meters)
/// [3-5]:   Velocity (Vx, Vy, Vz) in world frame (m/s)
/// [6-9]:   Quaternion (qw, qx, qy, qz) for attitude
/// [10-12]: Gyro Bias (bx, by, bz) in body frame (rad/s)
///
/// Runs in passive shadow mode alongside the main 8D filter.
/// Does NOT feed back into ZUPT or dashboard logic.

use ndarray::{arr1, Array1, Array2};
use serde::{Deserialize, Serialize};

const G: f64 = 9.81; // Earth gravity (m/s²)
const EARTH_RADIUS: f64 = 6.371e6; // meters

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Ekf13dState {
    /// Position in local frame (East, North, Up) relative to origin [meters]
    pub position: (f64, f64, f64),

    /// Velocity in world frame [m/s]
    pub velocity: (f64, f64, f64),

    /// Quaternion (w, x, y, z) representing attitude
    pub quaternion: (f64, f64, f64, f64),

    /// Gyro bias estimate [rad/s]
    pub gyro_bias: (f64, f64, f64),

    /// Covariance trace for uncertainty
    pub covariance_trace: f64,

    /// Update counters
    pub gps_updates: u64,
    pub accel_updates: u64,
    pub gyro_updates: u64,
}

pub struct Ekf13d {
    /// Time step [seconds]
    dt: f64,

    /// State vector [13D]
    state: Array1<f64>,

    /// Covariance matrix [13x13]
    covariance: Array2<f64>,

    /// Process noise matrix [13x13]
    process_noise: Array2<f64>,

    /// GPS measurement noise (position) [m²]
    r_gps: f64,

    /// Accelerometer measurement noise [m²/s⁴]
    r_accel: f64,

    /// Gyroscope measurement noise [rad²/s²]
    r_gyro: f64,

    /// Origin for local frame (lat, lon)
    origin: Option<(f64, f64)>,

    /// Update counters
    gps_updates: u64,
    accel_updates: u64,
    gyro_updates: u64,
}

impl Ekf13d {
    /// Create a new 13D EKF
    pub fn new(
        dt: f64,
        gps_noise_std: f64,
        accel_noise_std: f64,
        gyro_noise_std: f64,
    ) -> Self {
        let state = Array1::<f64>::zeros(13);
        let covariance = Self::default_covariance();
        let process_noise = Self::build_process_noise(dt, accel_noise_std, gyro_noise_std);

        Self {
            dt,
            state,
            covariance,
            process_noise,
            r_gps: gps_noise_std * gps_noise_std,
            r_accel: accel_noise_std * accel_noise_std,
            r_gyro: gyro_noise_std * gyro_noise_std,
            origin: None,
            gps_updates: 0,
            accel_updates: 0,
            gyro_updates: 0,
        }
    }

    fn default_covariance() -> Array2<f64> {
        let mut p = Array2::<f64>::zeros((13, 13));
        let diag = [
            100.0,  // pos_x
            100.0,  // pos_y
            100.0,  // pos_z
            10.0,   // vel_x
            10.0,   // vel_y
            10.0,   // vel_z
            0.1,    // qw
            0.1,    // qx
            0.1,    // qy
            0.1,    // qz
            0.01,   // bias_x
            0.01,   // bias_y
            0.01,   // bias_z
        ];
        for (idx, value) in diag.iter().enumerate() {
            p[[idx, idx]] = *value;
        }
        p
    }

    fn build_process_noise(dt: f64, accel_std: f64, gyro_std: f64) -> Array2<f64> {
        let mut q = Array2::<f64>::zeros((13, 13));

        let q_pos = 0.25 * dt.powi(4) * accel_std.powi(2);
        let q_vel = dt.powi(2) * accel_std.powi(2);
        let q_accel = accel_std.powi(2);
        let q_gyro = gyro_std.powi(2);

        // Position process noise
        q[[0, 0]] = q_pos;
        q[[1, 1]] = q_pos;
        q[[2, 2]] = q_pos;

        // Velocity process noise
        q[[3, 3]] = q_vel;
        q[[4, 4]] = q_vel;
        q[[5, 5]] = q_vel;

        // Quaternion process noise (from accel)
        q[[6, 6]] = q_accel * 0.001;
        q[[7, 7]] = q_accel * 0.001;
        q[[8, 8]] = q_accel * 0.001;
        q[[9, 9]] = q_accel * 0.001;

        // Gyro bias process noise (very small, bias slowly varying)
        q[[10, 10]] = q_gyro * 0.0001;
        q[[11, 11]] = q_gyro * 0.0001;
        q[[12, 12]] = q_gyro * 0.0001;

        q
    }

    /// Quaternion normalization
    fn normalize_quat(q: &mut [f64]) {
        let norm = (q[0].powi(2) + q[1].powi(2) + q[2].powi(2) + q[3].powi(2)).sqrt();
        if norm > 1e-6 {
            for qi in q.iter_mut() {
                *qi /= norm;
            }
        }
    }

    /// Rotate vector from body frame to world frame using quaternion
    fn rotate_body_to_world(quat: &[f64], body_vec: &[f64; 3]) -> [f64; 3] {
        // quat = [w, x, y, z]
        let (w, x, y, z) = (quat[0], quat[1], quat[2], quat[3]);

        // Rotation matrix from quaternion
        let r00 = 1.0 - 2.0 * (y.powi(2) + z.powi(2));
        let r01 = 2.0 * (x * y - w * z);
        let r02 = 2.0 * (x * z + w * y);

        let r10 = 2.0 * (x * y + w * z);
        let r11 = 1.0 - 2.0 * (x.powi(2) + z.powi(2));
        let r12 = 2.0 * (y * z - w * x);

        let r20 = 2.0 * (x * z - w * y);
        let r21 = 2.0 * (y * z + w * x);
        let r22 = 1.0 - 2.0 * (x.powi(2) + y.powi(2));

        [
            r00 * body_vec[0] + r01 * body_vec[1] + r02 * body_vec[2],
            r10 * body_vec[0] + r11 * body_vec[1] + r12 * body_vec[2],
            r20 * body_vec[0] + r21 * body_vec[1] + r22 * body_vec[2],
        ]
    }

    /// Prediction step with accel and gyro
    pub fn predict(&mut self, accel_body: (f64, f64, f64), gyro: (f64, f64, f64)) {
        // State indices:
        // [0-2]: position, [3-5]: velocity, [6-9]: quaternion, [10-12]: gyro bias

        // Get current state
        let pos = [self.state[0], self.state[1], self.state[2]];
        let vel = [self.state[3], self.state[4], self.state[5]];
        let mut quat = [self.state[6], self.state[7], self.state[8], self.state[9]];
        let bias = [self.state[10], self.state[11], self.state[12]];

        // Rotation: Body accel -> World accel (minus gravity)
        let accel_world = Self::rotate_body_to_world(&quat, &[accel_body.0, accel_body.1, accel_body.2]);

        // Gravity correction (gravity acts downward in world frame)
        let accel_corrected = [
            accel_world[0],
            accel_world[1],
            accel_world[2] - G,
        ];

        // Kinematics: Position += Velocity * dt
        let new_pos = [
            pos[0] + vel[0] * self.dt,
            pos[1] + vel[1] * self.dt,
            pos[2] + vel[2] * self.dt,
        ];

        // Kinematics: Velocity += Accel * dt
        let new_vel = [
            vel[0] + accel_corrected[0] * self.dt,
            vel[1] + accel_corrected[1] * self.dt,
            vel[2] + accel_corrected[2] * self.dt,
        ];

        // Quaternion integration: q_new = q + 0.5 * q * (gyro - bias) * dt
        // Simplified: q += 0.5 * [0, wx, wy, wz] * q * dt
        let gyro_corrected = [
            gyro.0 - bias[0],
            gyro.1 - bias[1],
            gyro.2 - bias[2],
        ];

        let dq_factor = 0.5 * self.dt;
        let dq = [
            -dq_factor * (gyro_corrected[0] * quat[1] + gyro_corrected[1] * quat[2] + gyro_corrected[2] * quat[3]),
            dq_factor * (gyro_corrected[0] * quat[0] - gyro_corrected[1] * quat[3] + gyro_corrected[2] * quat[2]),
            dq_factor * (gyro_corrected[1] * quat[0] + gyro_corrected[0] * quat[3] - gyro_corrected[2] * quat[1]),
            dq_factor * (gyro_corrected[2] * quat[0] - gyro_corrected[0] * quat[2] + gyro_corrected[1] * quat[1]),
        ];

        for i in 0..4 {
            quat[i] += dq[i];
        }

        Self::normalize_quat(&mut quat);

        // Gyro bias stays constant (no process model)

        // Update state
        self.state[0] = new_pos[0];
        self.state[1] = new_pos[1];
        self.state[2] = new_pos[2];
        self.state[3] = new_vel[0];
        self.state[4] = new_vel[1];
        self.state[5] = new_vel[2];
        self.state[6] = quat[0];
        self.state[7] = quat[1];
        self.state[8] = quat[2];
        self.state[9] = quat[3];
        // Bias unchanged

        // Covariance prediction: P = F * P * F^T + Q
        // Simplified: P += Q (full F matrix omitted for shadow mode)
        self.covariance = &self.covariance + &self.process_noise;

        self.accel_updates += 1;

        // Track gyro updates when gyro data is non-zero
        if gyro.0.abs() > 1e-6 || gyro.1.abs() > 1e-6 || gyro.2.abs() > 1e-6 {
            self.gyro_updates += 1;
        }
    }

    /// GPS update step (position only)
    pub fn update_gps(&mut self, lat: f64, lon: f64, origin_lat: f64, origin_lon: f64) {
        if self.origin.is_none() {
            self.origin = Some((origin_lat, origin_lon));
        }

        // Convert GPS (lat, lon) to local (east, north) relative to origin
        let (origin_lat_stored, origin_lon_stored) = self.origin.unwrap();
        let (meas_east, meas_north) = self.latlonalt_to_local(lat, lon, origin_lat_stored, origin_lon_stored);

        // Measurement: [pos_x, pos_y]
        let residual = arr1(&[
            meas_east - self.state[0],
            meas_north - self.state[1],
        ]);

        // Measurement matrix H (2x13)
        let mut h = Array2::<f64>::zeros((2, 13));
        h[[0, 0]] = 1.0;  // pos_x
        h[[1, 1]] = 1.0;  // pos_y

        // Innovation covariance: S = H * P * H^T + R
        let ph_t = self.covariance.dot(&h.t());
        let hph_t = h.dot(&ph_t);
        let mut s = hph_t;
        s[[0, 0]] += self.r_gps;
        s[[1, 1]] += self.r_gps;

        // Kalman gain: K = P * H^T * S^-1
        let s_inv = Self::invert_2x2(&s);
        let k = ph_t.dot(&s_inv);

        // State update: x += K * residual
        let dx = k.dot(&residual);
        for i in 0..13 {
            self.state[i] += dx[i];
        }

        // Re-normalize quaternion after update
        let mut quat = [self.state[6], self.state[7], self.state[8], self.state[9]];
        Self::normalize_quat(&mut quat);
        self.state[6] = quat[0];
        self.state[7] = quat[1];
        self.state[8] = quat[2];
        self.state[9] = quat[3];

        // Covariance update: P = (I - K*H) * P
        let kh = k.dot(&h);
        let i_minus_kh = {
            let mut i = Array2::<f64>::eye(13);
            for i_idx in 0..13 {
                for j_idx in 0..13 {
                    i[[i_idx, j_idx]] -= kh[[i_idx, j_idx]];
                }
            }
            i
        };
        self.covariance = i_minus_kh.dot(&self.covariance);

        self.gps_updates += 1;
    }

    /// Gyroscope update (heading refinement via inclinometer concept)
    /// For now, this is a placeholder that does nothing.
    pub fn update_gyro(&mut self, _gyro: (f64, f64, f64)) {
        // Future: estimate gyro bias correction
        self.gyro_updates += 1;
    }

    /// Initialize GPS origin and position
    pub fn set_origin(&mut self, lat: f64, lon: f64) {
        self.origin = Some((lat, lon));
        self.state[0] = 0.0;
        self.state[1] = 0.0;
        self.state[2] = 0.0;
    }

    /// Check if origin has been initialized
    pub fn is_origin_set(&self) -> bool {
        self.origin.is_some()
    }

    /// Initialize quaternion (assumes level start)
    pub fn set_initial_quaternion(&mut self, yaw_rad: f64) {
        // Quaternion from yaw only (roll=0, pitch=0)
        let half_yaw = yaw_rad * 0.5;
        self.state[6] = half_yaw.cos();      // w
        self.state[7] = 0.0;                 // x
        self.state[8] = 0.0;                 // y
        self.state[9] = half_yaw.sin();      // z
    }

    /// Get current state snapshot
    pub fn get_state(&self) -> Ekf13dState {
        Ekf13dState {
            position: (self.state[0], self.state[1], self.state[2]),
            velocity: (self.state[3], self.state[4], self.state[5]),
            quaternion: (self.state[6], self.state[7], self.state[8], self.state[9]),
            gyro_bias: (self.state[10], self.state[11], self.state[12]),
            covariance_trace: (0..13).map(|i| self.covariance[[i, i]]).sum(),
            gps_updates: self.gps_updates,
            accel_updates: self.accel_updates,
            gyro_updates: self.gyro_updates,
        }
    }

    /// Convert lat/lon to local east/north relative to origin
    fn latlonalt_to_local(&self, lat: f64, lon: f64, origin_lat: f64, origin_lon: f64) -> (f64, f64) {
        let lat_rad = lat.to_radians();
        let lon_rad = lon.to_radians();
        let origin_lat_rad = origin_lat.to_radians();
        let origin_lon_rad = origin_lon.to_radians();

        let dlat = lat_rad - origin_lat_rad;
        let dlon = lon_rad - origin_lon_rad;

        let north = EARTH_RADIUS * dlat;
        let east = EARTH_RADIUS * dlon * origin_lat_rad.cos();

        (east, north)
    }

    /// Simple 2x2 matrix inversion
    fn invert_2x2(m: &Array2<f64>) -> Array2<f64> {
        let det = m[[0, 0]] * m[[1, 1]] - m[[0, 1]] * m[[1, 0]];
        if det.abs() < 1e-12 {
            return Array2::<f64>::eye(2);
        }

        let mut inv = Array2::<f64>::zeros((2, 2));
        inv[[0, 0]] = m[[1, 1]] / det;
        inv[[0, 1]] = -m[[0, 1]] / det;
        inv[[1, 0]] = -m[[1, 0]] / det;
        inv[[1, 1]] = m[[0, 0]] / det;

        inv
    }
}
