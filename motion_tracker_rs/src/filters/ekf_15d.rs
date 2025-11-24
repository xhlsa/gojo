#![allow(dead_code)]

use nalgebra::{Matrix3, MatrixMN, Vector3, U15, U3};
use ndarray::s;
/// 15-Dimensional Extended Kalman Filter (Full IMU Bias Estimation)
///
/// State Vector (15D):
/// [0-2]:   Position (X, Y, Z) in local frame (meters)
/// [3-5]:   Velocity (Vx, Vy, Vz) in world frame (m/s)
/// [6-9]:   Quaternion (qw, qx, qy, qz) for attitude
/// [10-12]: Gyro Bias (bx, by, bz) in body frame (rad/s)
/// [13-14]: Accel Bias (bx, by, bz) in body frame (m/s²) -- NEW
///
/// Extends 13D EKF with accelerometer bias estimation for better long-term accuracy.
/// Runs in shadow mode alongside main filter.
use ndarray::{arr1, Array1, Array2};
use serde::{Deserialize, Serialize};

const G: f64 = 9.81; // Earth gravity (m/s²)

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Ekf15dState {
    /// Position in local frame (East, North, Up) relative to origin [meters]
    pub position: (f64, f64, f64),

    /// Velocity in world frame [m/s]
    pub velocity: (f64, f64, f64),

    /// Quaternion (w, x, y, z) representing attitude
    pub quaternion: (f64, f64, f64, f64),

    /// Gyro bias estimate [rad/s]
    pub gyro_bias: (f64, f64, f64),

    /// Accel bias estimate [m/s²]
    pub accel_bias: (f64, f64, f64),

    /// Covariance trace for uncertainty
    pub covariance_trace: f64,

    /// Update counters
    pub gps_updates: u64,
    pub accel_updates: u64,
    pub gyro_updates: u64,
}

pub struct Ekf15d {
    /// Time step [seconds]
    dt: f64,

    /// State vector [15D]
    state: Array1<f64>,

    /// Covariance matrix [15x15]
    covariance: Array2<f64>,

    /// Process noise matrix [15x15]
    process_noise: Array2<f64>,

    /// GPS measurement noise (position) [m²]
    r_gps: f64,

    /// Accelerometer measurement noise [m²/s⁴]
    r_accel: f64,

    /// Gyroscope measurement noise [rad²/s²]
    r_gyro: f64,

    /// Accel bias process noise (random walk) [m²/s⁴]
    q_accel_bias: f64,

    /// Origin for local frame (lat, lon)
    origin: Option<(f64, f64)>,

    /// Update counters
    gps_updates: u64,
    accel_updates: u64,
    gyro_updates: u64,
}

impl Ekf15d {
    /// Create a new 15D EKF
    pub fn new(dt: f64, gps_noise_std: f64, accel_noise_std: f64, gyro_noise_std: f64) -> Self {
        let mut state = Array1::<f64>::zeros(15);
        // Initialize quaternion to identity
        state[6] = 1.0;

        // Initialize covariance (15x15)
        let mut covariance = Array2::<f64>::zeros((15, 15));
        let diag = [
            100.0, 100.0, 100.0, // position: 100 m² uncertainty
            10.0, 10.0, 10.0, // velocity: 10 m²/s² uncertainty
            1.0, 1.0, 1.0, 1.0, // quaternion: 1.0 (unitless)
            0.1, 0.1, 0.1, // gyro bias: 0.1 rad²/s²
            0.1, 0.1, // accel bias (x, y): assume stable sensors at start
        ];
        for (i, &val) in diag.iter().enumerate() {
            covariance[[i, i]] = val;
        }

        // Process noise matrix
        let mut process_noise = Array2::<f64>::zeros((15, 15));
        let accel_var = accel_noise_std * accel_noise_std;
        let gyro_var = gyro_noise_std * gyro_noise_std;

        // Position: constant velocity model (continuous white noise acceleration)
        let q_pos = 0.25 * dt.powi(4) * accel_var;
        for i in 0..3 {
            process_noise[[i, i]] = q_pos;
        }

        // Velocity: driven by accel noise
        let q_vel = dt.powi(2) * accel_var;
        for i in 3..6 {
            process_noise[[i, i]] = q_vel;
        }

        // Quaternion: stable (integrated from gyro, handled in predict)
        for i in 6..10 {
            process_noise[[i, i]] = gyro_var * dt * dt;
        }

        // Gyro bias: random walk (extremely stable)
        let q_gyro_bias = 1e-8;
        for i in 10..13 {
            process_noise[[i, i]] = q_gyro_bias;
        }

        // Accel bias: random walk (extremely stable)
        let q_accel_bias = 1e-7;
        for i in 13..15 {
            process_noise[[i, i]] = q_accel_bias;
        }

        Self {
            dt,
            state,
            covariance,
            process_noise,
            r_gps: gps_noise_std * gps_noise_std,
            r_accel: accel_noise_std * accel_noise_std,
            r_gyro: gyro_noise_std * gyro_noise_std,
            q_accel_bias,
            origin: None,
            gps_updates: 0,
            accel_updates: 0,
            gyro_updates: 0,
        }
    }

    /// Get current state
    pub fn get_state(&self) -> Ekf15dState {
        Ekf15dState {
            position: (self.state[0], self.state[1], self.state[2]),
            velocity: (self.state[3], self.state[4], self.state[5]),
            quaternion: (self.state[6], self.state[7], self.state[8], self.state[9]),
            gyro_bias: (self.state[10], self.state[11], self.state[12]),
            accel_bias: (self.state[13], self.state[14], 0.0), // Z-accel bias (placeholder for symmetry)
            covariance_trace: self.covariance.diag().sum(),
            gps_updates: self.gps_updates,
            accel_updates: self.accel_updates,
            gyro_updates: self.gyro_updates,
        }
    }

    /// Predict step: integrate kinematics with bias correction
    pub fn predict(&mut self, accel_raw: (f64, f64, f64), gyro_raw: (f64, f64, f64)) {
        // Get biases from state
        let gyro_bias = [self.state[10], self.state[11], self.state[12]];
        let accel_bias = [self.state[13], self.state[14], 0.0]; // Z-axis accel bias placeholder

        // Correct measurements
        let accel_corr = [
            accel_raw.0 - accel_bias[0],
            accel_raw.1 - accel_bias[1],
            accel_raw.2 - accel_bias[2],
        ];
        let gyro_corr = [
            gyro_raw.0 - gyro_bias[0],
            gyro_raw.1 - gyro_bias[1],
            gyro_raw.2 - gyro_bias[2],
        ];

        // Current state
        let mut pos = [self.state[0], self.state[1], self.state[2]];
        let mut vel = [self.state[3], self.state[4], self.state[5]];
        let mut quat = [self.state[6], self.state[7], self.state[8], self.state[9]];

        // Quaternion integration (simple exponential map)
        let gyro_mag = (gyro_corr[0] * gyro_corr[0]
            + gyro_corr[1] * gyro_corr[1]
            + gyro_corr[2] * gyro_corr[2])
            .sqrt();

        if gyro_mag > 1e-6 {
            let half_angle = 0.5 * gyro_mag * self.dt;
            let scale = half_angle.sin() / gyro_mag;

            let dq = [
                half_angle.cos(),
                gyro_corr[0] * scale,
                gyro_corr[1] * scale,
                gyro_corr[2] * scale,
            ];

            // Quaternion multiplication: q_new = dq * q
            let qw = dq[0] * quat[0] - dq[1] * quat[1] - dq[2] * quat[2] - dq[3] * quat[3];
            let qx = dq[0] * quat[1] + dq[1] * quat[0] + dq[2] * quat[3] - dq[3] * quat[2];
            let qy = dq[0] * quat[2] - dq[1] * quat[3] + dq[2] * quat[0] + dq[3] * quat[1];
            let qz = dq[0] * quat[3] + dq[1] * quat[2] - dq[2] * quat[1] + dq[3] * quat[0];

            quat = [qw, qx, qy, qz];

            // Normalize quaternion
            let quat_mag =
                (quat[0] * quat[0] + quat[1] * quat[1] + quat[2] * quat[2] + quat[3] * quat[3])
                    .sqrt();
            if quat_mag > 1e-6 {
                quat[0] /= quat_mag;
                quat[1] /= quat_mag;
                quat[2] /= quat_mag;
                quat[3] /= quat_mag;
            }
        }

        // Rotate accel to world frame using quaternion
        // World accel = R^T * accel_body - [0, 0, g]
        let accel_world = rotate_accel_to_world(&quat, &accel_corr);

        // Update velocity: v += (a - g) * dt
        vel[0] += accel_world[0] * self.dt;
        vel[1] += accel_world[1] * self.dt;
        vel[2] += (accel_world[2] - G) * self.dt;

        // Update position: p += v * dt
        pos[0] += vel[0] * self.dt;
        pos[1] += vel[1] * self.dt;
        pos[2] += vel[2] * self.dt;

        // Update state
        self.state[0] = pos[0];
        self.state[1] = pos[1];
        self.state[2] = pos[2];
        self.state[3] = vel[0];
        self.state[4] = vel[1];
        self.state[5] = vel[2];
        self.state[6] = quat[0];
        self.state[7] = quat[1];
        self.state[8] = quat[2];
        self.state[9] = quat[3];
        // Biases held constant (updated by measurement corrections)

        // Update covariance: P = F*P*F^T + Q (simplified)
        self.covariance = self.covariance.clone() + self.process_noise.clone();
    }

    /// GPS update: correct position
    pub fn update_gps(&mut self, gps_pos: (f64, f64, f64)) {
        let (mut pos_x, mut pos_y, mut pos_z) = gps_pos;
        if let Some((origin_lat, origin_lon)) = self.origin {
            let (x, y) = latlon_to_meters(pos_x, pos_y, origin_lat, origin_lon);
            pos_x = x;
            pos_y = y;
        }

        // Simple measurement update for position [0-2]
        let innovation = [
            pos_x - self.state[0],
            pos_y - self.state[1],
            pos_z - self.state[2],
        ];

        // Measurement matrix H (identity for position)
        let mut h = Array2::<f64>::zeros((3, 15));
        for i in 0..3 {
            h[[i, i]] = 1.0;
        }

        // Innovation covariance: S = H*P*H^T + R
        let mut s = Array2::<f64>::zeros((3, 3));
        for i in 0..3 {
            for j in 0..3 {
                s[[i, j]] = self.covariance[[i, j]];
                if i == j {
                    s[[i, j]] += self.r_gps;
                }
            }
        }

        // Kalman gain: K = P*H^T*S^-1 (simplified for diagonal case)
        for i in 0..3 {
            if s[[i, i]].abs() > 1e-6 {
                let gain = self.covariance[[i, i]] / s[[i, i]];
                self.state[i] += gain * innovation[i];

                // Update covariance: P = (I - K*H)*P
                self.covariance[[i, i]] *= 1.0 - gain;
            }
        }

        self.gps_updates += 1;
    }

    /// Set local origin for GPS conversion and reset position
    pub fn set_origin(&mut self, lat: f64, lon: f64, _alt: f64) {
        self.origin = Some((lat, lon));
        self.state[0] = 0.0;
        self.state[1] = 0.0;
        self.state[2] = 0.0;
    }

    /// Accelerometer update: correct velocity and accel bias
    pub fn update_accel(&mut self, accel_meas: (f64, f64, f64)) {
        // Rotate accel measurement to world frame
        let quat = [self.state[6], self.state[7], self.state[8], self.state[9]];
        let accel_world = rotate_accel_to_world(&quat, &[accel_meas.0, accel_meas.1, accel_meas.2]);

        // Expected accel from model: a_expected = a_measured - g (vertical)
        let mut innovation = [accel_world[0], accel_world[1], accel_world[2] - G];

        // Accel bias affects measurement (reduce innovation)
        innovation[0] -= self.state[13];
        innovation[1] -= self.state[14];

        // Update accel bias (simple proportional gain)
        let gain = 0.001; // Learning rate for bias
        self.state[13] -= gain * innovation[0];
        self.state[14] -= gain * innovation[1];

        self.accel_updates += 1;
    }

    /// Gyro update: used for quaternion validation
    pub fn update_gyro(&mut self, _gyro_meas: (f64, f64, f64)) {
        // Gyro is already used in predict, no additional measurement update needed
        self.gyro_updates += 1;
    }

    /// Force velocity state to zero (used for ZUPT / stationary clamping)
    pub fn force_zero_velocity(&mut self) {
        self.state[3] = 0.0;
        self.state[4] = 0.0;
        self.state[5] = 0.0;
    }

    /// Velocity update with small noise to shrink covariance when GPS reports stationary.
    pub fn update_velocity(&mut self, velocity: (f64, f64, f64), noise_var: f64) {
        let measurement = [velocity.0, velocity.1, velocity.2];
        for i in 0..3 {
            let idx = 3 + i;
            let innovation = measurement[i] - self.state[idx];
            let s = self.covariance[[idx, idx]] + noise_var;
            if s.abs() > 1e-12 {
                let gain = self.covariance[[idx, idx]] / s;
                self.state[idx] += gain * innovation;
                self.covariance[[idx, idx]] *= 1.0 - gain;
            }
        }
    }

    /// Non-holonomic body-frame velocity constraint (constrains lateral/vertical drift)
    pub fn update_body_velocity(&mut self, measurement: Vector3<f64>) {
        // Rotation matrix from body to world (transpose used to project world velocity into body frame)
        let mut qw = self.state[6];
        let mut qx = self.state[7];
        let mut qy = self.state[8];
        let mut qz = self.state[9];

        // Normalize quaternion to avoid scaling artifacts
        let q_norm = (qw * qw + qx * qx + qy * qy + qz * qz).sqrt();
        if q_norm > 1e-9 {
            qw /= q_norm;
            qx /= q_norm;
            qy /= q_norm;
            qz /= q_norm;
        } else {
            qw = 1.0;
            qx = 0.0;
            qy = 0.0;
            qz = 0.0;
        }

        let r00 = 1.0 - 2.0 * (qy * qy + qz * qz);
        let r01 = 2.0 * (qx * qy - qw * qz);
        let r02 = 2.0 * (qx * qz + qw * qy);

        let r10 = 2.0 * (qx * qy + qw * qz);
        let r11 = 1.0 - 2.0 * (qx * qx + qz * qz);
        let r12 = 2.0 * (qy * qz - qw * qx);

        let r20 = 2.0 * (qx * qz - qw * qy);
        let r21 = 2.0 * (qy * qz + qw * qx);
        let r22 = 1.0 - 2.0 * (qx * qx + qy * qy);

        // R_body_from_world = R^T
        let h_vel =
            Array2::from_shape_vec((3, 3), vec![r00, r10, r20, r01, r11, r21, r02, r12, r22])
                .unwrap();

        // Predicted body-frame velocity
        let v_world = arr1(&[self.state[3], self.state[4], self.state[5]]);
        let v_body_pred = h_vel.dot(&v_world);

        // Innovation y = z - H * x
        let meas = arr1(&[measurement.x, measurement.y, measurement.z]);
        let innovation = &meas - &v_body_pred;

        // Measurement noise (ignore X, constrain Y/Z)
        let mut r = Matrix3::zeros();
        r[(0, 0)] = 999.0;
        r[(1, 1)] = 0.1;
        r[(2, 2)] = 0.1;

        // Extract velocity covariance block P_vv (3x3)
        let p_vv = self.covariance.slice(s![3..6, 3..6]).to_owned();
        let p_vv_mat = Matrix3::from_row_slice(p_vv.as_slice().unwrap());

        // Compute S = H * P_vv * H^T + R
        let h_mat = Matrix3::from_row_slice(h_vel.as_slice().unwrap());
        let s_mat = h_mat * p_vv_mat * h_mat.transpose() + r;

        if let Some(s_inv) = s_mat.try_inverse() {
            // P[:, vel] (15 x 3)
            let p_vel = self.covariance.slice(s![.., 3..6]).to_owned();
            // K = P * H^T * S^-1
            let h_t = h_mat.transpose();
            let mut h_t_arr = Array2::<f64>::zeros((3, 3));
            for i in 0..3 {
                for j in 0..3 {
                    h_t_arr[[i, j]] = h_t[(i, j)];
                }
            }
            let mut s_inv_arr = Array2::<f64>::zeros((3, 3));
            for r in 0..3 {
                for c in 0..3 {
                    s_inv_arr[[r, c]] = s_inv[(r, c)];
                }
            }
            let k_mat = p_vel.dot(&h_t_arr);
            let k = k_mat.dot(&s_inv_arr); // (15 x 3)

            // State update: x = x + K * innovation
            let dx = k.dot(&innovation);
            for i in 0..self.state.len() {
                self.state[i] += dx[i];
            }

            // Covariance update (Joseph form)
            let mut h_full = Array2::<f64>::zeros((3, self.state.len()));
            // place H in velocity columns
            for row in 0..3 {
                for col in 0..3 {
                    h_full[[row, 3 + col]] = h_vel[[row, col]];
                }
            }

            // Build nalgebra representations
            let k_na = MatrixMN::<f64, U15, U3>::from_row_slice(
                k.as_slice().expect("Kalman gain slice should exist"),
            );
            let h_na = MatrixMN::<f64, U3, U15>::from_row_slice(
                h_full.as_slice().expect("H slice should exist"),
            );
            let r_na = Matrix3::from_row_slice(r.as_slice());
            let p_na = MatrixMN::<f64, U15, U15>::from_row_slice(
                self.covariance
                    .as_slice()
                    .expect("Covariance slice should exist"),
            );
            let identity = MatrixMN::<f64, U15, U15>::identity();
            let i_minus_kh = identity - k_na.clone() * h_na.clone();
            let joseph =
                &i_minus_kh * p_na * i_minus_kh.transpose() + k_na * r_na * k_na.transpose();

            // copy back to ndarray and symmetrize
            let mut new_p = Array2::<f64>::zeros((self.state.len(), self.state.len()));
            for r in 0..self.state.len() {
                for c in 0..self.state.len() {
                    new_p[[r, c]] = joseph[(r, c)];
                }
            }
            // Symmetrize
            let mut sym_p = new_p.clone();
            for r in 0..self.state.len() {
                for c in 0..self.state.len() {
                    sym_p[[r, c]] = 0.5 * (new_p[[r, c]] + new_p[[c, r]]);
                }
            }
            self.covariance = sym_p;
        }
    }

    /// Get the current speed (velocity magnitude) from the 15D state
    pub fn get_speed(&self) -> f64 {
        let vx = self.state[3];
        let vy = self.state[4];
        let vz = self.state[5];
        (vx * vx + vy * vy + vz * vz).sqrt()
    }
}

fn latlon_to_meters(lat: f64, lon: f64, origin_lat: f64, origin_lon: f64) -> (f64, f64) {
    const R: f64 = 6_371_000.0;
    let d_lat = (lat - origin_lat).to_radians();
    let d_lon = (lon - origin_lon).to_radians();
    let x = R * d_lon * origin_lat.to_radians().cos();
    let y = R * d_lat;
    (x, y)
}

/// Rotate acceleration from body frame to world frame using quaternion
fn rotate_accel_to_world(quat: &[f64; 4], accel_body: &[f64; 3]) -> [f64; 3] {
    let qw = quat[0];
    let qx = quat[1];
    let qy = quat[2];
    let qz = quat[3];

    // Compute rotation matrix elements (only needed for rotation)
    let r00 = 1.0 - 2.0 * (qy * qy + qz * qz);
    let r01 = 2.0 * (qx * qy - qw * qz);
    let r02 = 2.0 * (qx * qz + qw * qy);

    let r10 = 2.0 * (qx * qy + qw * qz);
    let r11 = 1.0 - 2.0 * (qx * qx + qz * qz);
    let r12 = 2.0 * (qy * qz - qw * qx);

    let r20 = 2.0 * (qx * qz - qw * qy);
    let r21 = 2.0 * (qy * qz + qw * qx);
    let r22 = 1.0 - 2.0 * (qx * qx + qy * qy);

    // Rotation: a_world = R^T * a_body
    [
        r00 * accel_body[0] + r10 * accel_body[1] + r20 * accel_body[2],
        r01 * accel_body[0] + r11 * accel_body[1] + r21 * accel_body[2],
        r02 * accel_body[0] + r12 * accel_body[1] + r22 * accel_body[2],
    ]
}
