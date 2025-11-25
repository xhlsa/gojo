use nalgebra::{Matrix3, SMatrix, Vector3};
use ndarray::{arr1, s, Array1, Array2};
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
    pub dt: f64,

    /// State vector [15D]
    pub state: Array1<f64>,

    /// Covariance matrix [15x15]
    pub covariance: Array2<f64>,

    /// Process noise matrix [15x15]
    pub process_noise: Array2<f64>,

    /// GPS measurement noise (position) [m²]
    _r_gps: f64,

    /// Accelerometer measurement noise [m²/s⁴]
    r_accel: f64,

    /// Gyroscope measurement noise [rad²/s²]
    r_gyro: f64,

    /// Accel bias process noise (random walk) [m²/s⁴]
    _q_accel_bias: f64,

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
        // Velocity process noise (tuned for responsiveness after ZUPT)
        let q_vel = 2.0;
        for i in 3..6 {
            process_noise[[i, i]] = q_vel;
        }

        // Quaternion: stable (integrated from gyro, handled in predict)
        for i in 6..10 {
            process_noise[[i, i]] = gyro_var * dt * dt;
        }

        // Gyro bias: random walk (LOCKED DOWN - prevent error dumping)
        let q_gyro_bias = 1e-8; // allow slow drift to avoid dumping error into velocity
        for i in 10..13 {
            process_noise[[i, i]] = q_gyro_bias;
        }

        // Accel bias: random walk (LOCKED DOWN - prevent error dumping)
        let q_accel_bias = 1e-8; // allow small adaptation to sensor drift
        for i in 13..15 {
            process_noise[[i, i]] = q_accel_bias;
        }

        Self {
            dt,
            state,
            covariance,
            process_noise,
            _r_gps: gps_noise_std * gps_noise_std,
            r_accel: accel_noise_std * accel_noise_std,
            r_gyro: gyro_noise_std * gyro_noise_std,
            _q_accel_bias: q_accel_bias,
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

        // ===== ERROR-STATE JACOBIAN (Restored) =====
        let dim = self.state.len();
        let mut f = Array2::<f64>::eye(dim);
        let r_mat = quat_to_rotation_matrix(&quat);

        // 1. Position depends on Velocity
        f[[0, 3]] = self.dt;
        f[[1, 4]] = self.dt;
        f[[2, 5]] = self.dt;

        // 2. Velocity depends on Attitude Error (scaled coupling)
        // dV/dTheta = -R * [a_body]x * dt * coupling_scale
        let coupling_scale = 0.2; // damped to avoid instability
        let a_skew = skew_symmetric(&[accel_corr[0], accel_corr[1], accel_corr[2]]);
        let dv_dtheta = r_mat.dot(&a_skew) * -self.dt * coupling_scale;

        // Map 3D rotation error to indices 6,7,8
        for r in 0..3 {
            for c in 0..3 {
                f[[3 + r, 6 + c]] = dv_dtheta[[r, c]];
            }
        }

        // 3. Velocity depends on Accel Bias (scaled)
        // dV/db_a = -R * dt * coupling_scale
        let dv_dba = &r_mat * -self.dt * coupling_scale;
        // Map to bias states 13 (bx), 14 (by).
        for r in 0..3 {
            f[[3 + r, 13]] = dv_dba[[r, 0]];
            f[[3 + r, 14]] = dv_dba[[r, 1]];
        }

        // 4. Attitude depends on Gyro Bias
        // dTheta/db_g = -I * dt
        f[[6, 10]] = -self.dt;
        f[[7, 11]] = -self.dt;
        f[[8, 12]] = -self.dt;

        // Propagate covariance: P = F * P * F^T + Q
        let fp = f.dot(&self.covariance);
        let fpf_t = fp.dot(&f.t());
        self.covariance = fpf_t + &self.process_noise;

        // Force symmetry
        let p_t = self.covariance.t();
        self.covariance = (&self.covariance + &p_t) * 0.5;
    }

    /// GPS update: correct position with accuracy-based gating
    pub fn update_gps(&mut self, gps_pos: (f64, f64, f64), accuracy: f64) {
        // STEP 3: Enforce GPS accuracy floor (minimum 5m)
        let gps_noise = (accuracy * accuracy).max(5.0 * 5.0);

        let (mut pos_x, mut pos_y, pos_z) = gps_pos;
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
                    s[[i, j]] += gps_noise;
                }
            }
        }

        // STEP 1: Tikhonov regularization
        for i in 0..3 {
            s[[i, i]] += 1e-6;
        }

        // Kalman gain: K = P*H^T*S^-1 (simplified for diagonal S)
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

    /// GPS velocity update: use speed + bearing to correct vx/vy
    pub fn update_gps_velocity(&mut self, speed: f64, bearing_rad: f64, speed_std: f64) {
        // Convert speed/bearing to ENU components (bearing: 0 = North, clockwise)
        let vx_meas = speed * bearing_rad.sin(); // East
        let vy_meas = speed * bearing_rad.cos(); // North
        let vz_meas = 0.0;

        let innovation = arr1(&[
            vx_meas - self.state[3],
            vy_meas - self.state[4],
            vz_meas - self.state[5],
        ]);

        // Measurement matrix maps velocity states [3,4,5]
        let mut h = Array2::<f64>::zeros((3, 15));
        h[[0, 3]] = 1.0;
        h[[1, 4]] = 1.0;
        h[[2, 5]] = 1.0;

        let mut r = Array2::<f64>::zeros((3, 3));
        let var = (speed_std * speed_std).max(0.0001); // trust GPS velocity more
        r[[0, 0]] = var;
        r[[1, 1]] = var;
        r[[2, 2]] = var * 2.0; // slight damp on vertical

        // Ensure velocity covariance is not crushed so GPS can influence it
        for i in 3..6 {
            self.covariance[[i, i]] = self.covariance[[i, i]].max(0.1);
        }

        let p = &self.covariance;
        let h_t = h.t();
        let s = h.dot(p).dot(&h_t) + r.clone();

        // Invert S (3x3)
        use nalgebra::Matrix3;
        let s_mat = Matrix3::new(
            s[[0, 0]],
            s[[0, 1]],
            s[[0, 2]],
            s[[1, 0]],
            s[[1, 1]],
            s[[1, 2]],
            s[[2, 0]],
            s[[2, 1]],
            s[[2, 2]],
        );
        if let Some(inv) = s_mat.try_inverse() {
            let mut s_inv = Array2::<f64>::zeros((3, 3));
            for r in 0..3 {
                for c in 0..3 {
                    s_inv[[r, c]] = inv[(r, c)];
                }
            }

            // Clamp extreme innovations to avoid runaway spikes
            let max_jump = 50.0;
            let mut innovation_clamped = innovation.clone();
            for i in 0..3 {
                innovation_clamped[i] = innovation_clamped[i].clamp(-max_jump, max_jump);
            }

            let k = p.dot(&h_t).dot(&s_inv);
            let dx = k.dot(&innovation_clamped);
            for i in 0..15 {
                self.state[i] += dx[i];
            }

            // Joseph form
            let i_mat = Array2::<f64>::eye(15);
            let kh = k.dot(&h);
            let term1 = (&i_mat - &kh).dot(p).dot(&(&i_mat - &kh).t());
            let term2 = k.dot(&r).dot(&k.t());
            self.covariance = term1 + term2;

            let p_t = self.covariance.t().to_owned();
            self.covariance = (&self.covariance + &p_t) / 2.0;
        }
    }

    /// Set local origin for GPS conversion and reset position
    pub fn set_origin(&mut self, lat: f64, lon: f64, _alt: f64) {
        self.origin = Some((lat, lon));
        self.state[0] = 0.0;
        self.state[1] = 0.0;
        self.state[2] = 0.0;
    }

    /// Accelerometer update: correct bias assuming STATIONARY (ZUPT)
    pub fn update_stationary_accel(&mut self, accel_meas: (f64, f64, f64)) {
        // Prediction: Accel = R^T * [0,0,G] + Bias
        let quat = [self.state[6], self.state[7], self.state[8], self.state[9]];
        let r_mat = quat_to_rotation_matrix(&quat); // Body to World (R)
        let r_t = r_mat.t(); // World to Body

        let g_vec = arr1(&[0.0, 0.0, G]);
        let expected_gravity_body = r_t.dot(&g_vec); // R^T * g

        let bias_x = self.state[13];
        let bias_y = self.state[14];
        let bias_z = 0.0; // Not estimating Z bias

        let pred_x = expected_gravity_body[0] + bias_x;
        let pred_y = expected_gravity_body[1] + bias_y;
        let pred_z = expected_gravity_body[2] + bias_z;

        let innovation = arr1(&[
            accel_meas.0 - pred_x,
            accel_meas.1 - pred_y,
            accel_meas.2 - pred_z,
        ]);

        // Jacobian H:
        // d(accel)/d(bias) = I
        // d(accel)/d(att_err) = Skew(R^T * g)
        let mut h = Array2::<f64>::zeros((3, 15));
        h[[0, 13]] = 1.0;
        h[[1, 14]] = 1.0;

        let g_body_skew = skew_symmetric(&[
            expected_gravity_body[0],
            expected_gravity_body[1],
            expected_gravity_body[2],
        ]);

        for r in 0..3 {
            for c in 0..3 {
                h[[r, 6 + c]] = g_body_skew[[r, c]];
            }
        }

        // Measurement Noise
        let mut r = Array2::<f64>::eye(3);
        r[[0, 0]] = self.r_accel;
        r[[1, 1]] = self.r_accel;
        r[[2, 2]] = self.r_accel;

        // Kalman Update (Joseph form to keep covariance consistent)
        let p = &self.covariance;
        let h_t = h.t();
        let s = h.dot(p).dot(&h_t) + r.clone();

        // Invert S (3x3) using nalgebra for robustness
        use nalgebra::Matrix3;
        let s_mat = Matrix3::new(
            s[[0, 0]],
            s[[0, 1]],
            s[[0, 2]],
            s[[1, 0]],
            s[[1, 1]],
            s[[1, 2]],
            s[[2, 0]],
            s[[2, 1]],
            s[[2, 2]],
        );
        let Some(inv) = s_mat.try_inverse() else {
            return; // Singular innovation covariance
        };
        let mut s_inv = Array2::<f64>::zeros((3, 3));
        for r in 0..3 {
            for c in 0..3 {
                s_inv[[r, c]] = inv[(r, c)];
            }
        }

        let k = p.dot(&h_t).dot(&s_inv);
        let dx = k.dot(&innovation);

        for i in 0..15 {
            self.state[i] += dx[i];
        }

        let i_mat = Array2::<f64>::eye(15);
        let kh = k.dot(&h);
        let i_minus_kh = &i_mat - &kh;
        let term1 = i_minus_kh.dot(p).dot(&i_minus_kh.t());
        let term2 = k.dot(&r).dot(&k.t());
        self.covariance = term1 + term2;

        // Symmetrize to limit numerical drift
        let p_t = self.covariance.t().to_owned();
        self.covariance = (&self.covariance + &p_t) / 2.0;

        self.accel_updates += 1;
    }

    /// Gyro update: correct bias assuming STATIONARY (ZUPT)
    pub fn update_stationary_gyro(&mut self, gyro_meas: (f64, f64, f64)) {
        // Prediction: Gyro = Bias
        // Innovation = Measured - Bias
        let innovation = arr1(&[
            gyro_meas.0 - self.state[10],
            gyro_meas.1 - self.state[11],
            gyro_meas.2 - self.state[12],
        ]);

        // H = Identity for bias states (10, 11, 12)
        let mut h = Array2::<f64>::zeros((3, 15));
        h[[0, 10]] = 1.0;
        h[[1, 11]] = 1.0;
        h[[2, 12]] = 1.0;

        let mut r = Array2::<f64>::eye(3);
        r[[0, 0]] = self.r_gyro;
        r[[1, 1]] = self.r_gyro;
        r[[2, 2]] = self.r_gyro;

        let p = &self.covariance;
        let h_t = h.t();
        let s = h.dot(p).dot(&h_t) + r.clone();

        // Invert 3x3 S
        use nalgebra::Matrix3;
        let s_mat = Matrix3::new(
            s[[0, 0]],
            s[[0, 1]],
            s[[0, 2]],
            s[[1, 0]],
            s[[1, 1]],
            s[[1, 2]],
            s[[2, 0]],
            s[[2, 1]],
            s[[2, 2]],
        );

        if let Some(inv) = s_mat.try_inverse() {
            let mut s_inv = Array2::<f64>::zeros((3, 3));
            for r in 0..3 {
                for c in 0..3 {
                    s_inv[[r, c]] = inv[(r, c)];
                }
            }

            let k = p.dot(&h_t).dot(&s_inv);
            let dx = k.dot(&innovation);

            for i in 0..15 {
                self.state[i] += dx[i];
            }

            // Joseph form keeps covariance PSD after bias updates
            let i_mat = Array2::<f64>::eye(15);
            let kh = k.dot(&h);
            let i_minus_kh = &i_mat - &kh;
            let term1 = i_minus_kh.dot(p).dot(&i_minus_kh.t());
            let term2 = k.dot(&r).dot(&k.t());
            self.covariance = term1 + term2;

            let p_t = self.covariance.t().to_owned();
            self.covariance = (&self.covariance + &p_t) / 2.0;
        }

        self.gyro_updates += 1;
    }

    /// Force velocity state to zero (used for ZUPT / stationary clamping)
    pub fn force_zero_velocity(&mut self) {
        self.state[3] = 0.0;
        self.state[4] = 0.0;
        self.state[5] = 0.0;
    }

    /// Zero-velocity update: clamp velocity and scrub covariance when stationary.
    pub fn apply_zupt(&mut self, current_accel: &nalgebra::Vector3<f64>) {
        self.force_zero_velocity();
        // Scrub velocity rows/cols to keep P consistent/PSD
        self.covariance.slice_mut(s![3..6, ..]).fill(0.0);
        self.covariance.slice_mut(s![.., 3..6]).fill(0.0);
        self.covariance[[3, 3]] = 1e-9;
        self.covariance[[4, 4]] = 1e-9;
        self.covariance[[5, 5]] = 1e-9;
        // Align gravity (roll/pitch) while keeping yaw
        self.align_orientation_to_gravity(current_accel);
        // Symmetrize after manual edits
        let p_t = self.covariance.t().to_owned();
        self.covariance = (&self.covariance + &p_t) / 2.0;
    }

    /// Velocity update with small noise to shrink covariance when GPS reports stationary.
    pub fn update_velocity(&mut self, velocity: (f64, f64, f64), noise_var: f64) {
        let meas = arr1(&[velocity.0, velocity.1, velocity.2]);
        let mut h = Array2::<f64>::zeros((3, 15));
        h[[0, 3]] = 1.0;
        h[[1, 4]] = 1.0;
        h[[2, 5]] = 1.0;

        let mut r = Array2::<f64>::eye(3);
        r[[0, 0]] = noise_var;
        r[[1, 1]] = noise_var;
        r[[2, 2]] = noise_var;

        let p = &self.covariance;
        let h_t = h.t();
        let s = h.dot(p).dot(&h_t) + r.clone();

        use nalgebra::Matrix3;
        let s_mat = Matrix3::new(
            s[[0, 0]],
            s[[0, 1]],
            s[[0, 2]],
            s[[1, 0]],
            s[[1, 1]],
            s[[1, 2]],
            s[[2, 0]],
            s[[2, 1]],
            s[[2, 2]],
        );
        let Some(inv) = s_mat.try_inverse() else {
            return;
        };

        let mut s_inv = Array2::<f64>::zeros((3, 3));
        for r_i in 0..3 {
            for c_i in 0..3 {
                s_inv[[r_i, c_i]] = inv[(r_i, c_i)];
            }
        }

        let k = p.dot(&h_t).dot(&s_inv);
        let innovation = &meas - &arr1(&[self.state[3], self.state[4], self.state[5]]);
        let dx = k.dot(&innovation);
        for i in 0..15 {
            self.state[i] += dx[i];
        }

        let i_mat = Array2::<f64>::eye(15);
        let kh = k.dot(&h);
        let i_minus_kh = &i_mat - &kh;
        let term1 = i_minus_kh.dot(p).dot(&i_minus_kh.t());
        let term2 = k.dot(&r).dot(&k.t());
        self.covariance = term1 + term2;

        let p_t = self.covariance.t().to_owned();
        self.covariance = (&self.covariance + &p_t) / 2.0;
    }

    /// Clamp vertical velocity to zero with a strong prior (land vehicle assumption).
    pub fn zero_vertical_velocity(&mut self, noise_var: f64) {
        self.update_velocity((self.state[3], self.state[4], 0.0), noise_var);
    }

    /// Approximate tilt-compensated magnetic heading update (loose correction).
    /// mag is in body frame (microtesla), declination_rad adjusts magnetic north to true north (positive east).
    pub fn update_mag_heading(
        &mut self,
        mag: &crate::types::MagData,
        declination_rad: f64,
    ) -> Option<f64> {
        // Reject bad magnitudes (Earth field ~25-65 uT)
        let mag_norm =
            (mag.x * mag.x + mag.y * mag.y + mag.z * mag.z).sqrt();
        if mag_norm < 20.0 || mag_norm > 80.0 {
            return None;
        }

        // Extract roll/pitch from quaternion
        let qw = self.state[6];
        let qx = self.state[7];
        let qy = self.state[8];
        let qz = self.state[9];
        let sinr_cosp = 2.0 * (qw * qx + qy * qz);
        let cosr_cosp = 1.0 - 2.0 * (qx * qx + qy * qy);
        let roll = sinr_cosp.atan2(cosr_cosp);

        let sinp = 2.0 * (qw * qy - qz * qx);
        let pitch = if sinp.abs() >= 1.0 {
            sinp.signum() * std::f64::consts::FRAC_PI_2
        } else {
            sinp.asin()
        };

        // Tilt compensation
        let (sin_r, cos_r) = (roll.sin(), roll.cos());
        let (sin_p, cos_p) = (pitch.sin(), pitch.cos());
        let mag_x_h = mag.x * cos_p + mag.y * sin_r * sin_p + mag.z * cos_r * sin_p;
        let mag_y_h = mag.y * cos_r - mag.z * sin_r;
        let mag_yaw = mag_y_h.atan2(mag_x_h) + declination_rad; // ENU yaw (CCW from East)

        // Extract current yaw
        let siny_cosp = 2.0 * (qw * qz + qx * qy);
        let cosy_cosp = 1.0 - 2.0 * (qy * qy + qz * qz);
        let current_yaw = siny_cosp.atan2(cosy_cosp);

        // Innovation with wrap
        let mut innov = mag_yaw - current_yaw;
        while innov > std::f64::consts::PI {
            innov -= 2.0 * std::f64::consts::PI;
        }
        while innov < -std::f64::consts::PI {
            innov += 2.0 * std::f64::consts::PI;
        }

        // Reject extreme innovations (>90 deg)
        if innov.abs() > std::f64::consts::FRAC_PI_2 {
            return None;
        }

        // Apply partial correction (poor-man's gain)
        let gain = 0.3;
        let new_yaw = current_yaw + gain * innov;
        let half = new_yaw * 0.5;
        let qw_new = half.cos();
        let qz_new = half.sin();
        self.state[6] = qw_new;
        self.state[7] = 0.0;
        self.state[8] = 0.0;
        self.state[9] = qz_new;

        Some(innov)
    }

    /// Clamp speed magnitude to a limit and scrub velocity covariance rows/cols.
    pub fn clamp_speed(&mut self, limit: f64) {
        if limit <= 0.0 {
            return;
        }
        let vx = self.state[3];
        let vy = self.state[4];
        let vz = self.state[5];
        let speed = (vx * vx + vy * vy + vz * vz).sqrt();
        if speed <= limit || speed < 1e-6 {
            return;
        }
        let scale = limit / speed;
        self.state[3] *= scale;
        self.state[4] *= scale;
        self.state[5] *= scale;

        // Reinforce velocity and position variance floors to avoid PSD issues
        for i in 3..6 {
            self.covariance[[i, i]] = self.covariance[[i, i]].max(1e-2);
        }
        for i in 0..3 {
            self.covariance[[i, i]] = self.covariance[[i, i]].max(1e-2);
        }
        // Gentle full-diagonal bump to keep P positive definite after aggressive scaling
        for i in 0..self.covariance.nrows() {
            self.covariance[[i, i]] += 1e-4;
        }
        // Symmetrize to reduce numerical drift
        let p_t = self.covariance.t().to_owned();
        self.covariance = (&self.covariance + &p_t) / 2.0;
    }

    /// Non-holonomic body-frame velocity constraint (constrains lateral/vertical drift)
    pub fn update_body_velocity(&mut self, measurement: Vector3<f64>, lateral_vertical_noise: f64) {
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
        let r_yz = lateral_vertical_noise.max(1e-6);
        r[(0, 0)] = 999.0;
        r[(1, 1)] = r_yz;
        r[(2, 2)] = r_yz;

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
            let k_na = SMatrix::<f64, 15, 3>::from_row_slice(
                k.as_slice().expect("Kalman gain slice should exist"),
            );
            let h_na = SMatrix::<f64, 3, 15>::from_row_slice(
                h_full.as_slice().expect("H slice should exist"),
            );
            let r_na = r;
            let p_na = SMatrix::<f64, 15, 15>::from_row_slice(
                self.covariance
                    .as_slice()
                    .expect("Covariance slice should exist"),
            );
            let identity = SMatrix::<f64, 15, 15>::identity();
            let i_minus_kh = identity - k_na.clone() * h_na.clone();

            // FIXED: Joseph form P = (I-KH)*P*(I-KH)^T + K*R*K^T
            // Explicit parentheses to ensure correct order
            let i_minus_kh_t = i_minus_kh.transpose();
            let term1_a = &i_minus_kh * p_na; // (I-KH) * P
            let term1 = term1_a * i_minus_kh_t; // ((I-KH)*P) * (I-KH)^T

            let term2_a = k_na.clone() * r_na; // K * R
            let term2 = term2_a * k_na.transpose(); // (K*R) * K^T

            let joseph = term1 + term2;

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

            // Ensure positive definiteness: clamp any negative variances to a small floor
            for i in 0..self.state.len() {
                if sym_p[[i, i]] < 1e-6 {
                    sym_p[[i, i]] = 1e-6;
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
    /// Align orientation to gravity while preserving yaw (ENU frame)
    pub fn align_orientation_to_gravity(&mut self, current_accel: &nalgebra::Vector3<f64>) {
        let accel_norm = current_accel.norm();
        if accel_norm < 0.1 || accel_norm.is_nan() {
            return; // Garbage data, skip alignment
        }

        let ax = current_accel.x;
        let ay = current_accel.y;
        let az = current_accel.z;

        // Roll/Pitch from accel (assuming ENU, gravity ~ -Z when level)
        let roll_acc = ay.atan2(az);
        let pitch_acc = (-ax).atan2((ay * ay + az * az).sqrt());

        // Extract current yaw from quaternion
        let q = nalgebra::UnitQuaternion::from_quaternion(nalgebra::Quaternion::new(
            self.state[6],
            self.state[7],
            self.state[8],
            self.state[9],
        ));
        let (_, _, yaw) = q.euler_angles(); // roll, pitch, yaw

        // Rebuild quaternion with preserved yaw, new roll/pitch
        let new_q = nalgebra::UnitQuaternion::from_euler_angles(roll_acc, pitch_acc, yaw);
        self.state[6] = new_q.w;
        self.state[7] = new_q.i;
        self.state[8] = new_q.j;
        self.state[9] = new_q.k;

        // Reset roll/pitch covariance (keep yaw covariance as-is)
        self.covariance.slice_mut(s![6..8, ..]).fill(0.0);
        self.covariance.slice_mut(s![.., 6..8]).fill(0.0);
        self.covariance[[6, 6]] = 1e-6;
        self.covariance[[7, 7]] = 1e-6;
    }
}

/// Convert lat/lon coordinates to local meters relative to origin
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

/// Compute skew-symmetric matrix for cross product (used in Jacobian)
fn skew_symmetric(v: &[f64; 3]) -> Array2<f64> {
    Array2::from_shape_vec(
        (3, 3),
        vec![0.0, -v[2], v[1], v[2], 0.0, -v[0], -v[1], v[0], 0.0],
    )
    .unwrap()
}

/// Compute rotation matrix from quaternion
fn quat_to_rotation_matrix(quat: &[f64; 4]) -> Array2<f64> {
    let qw = quat[0];
    let qx = quat[1];
    let qy = quat[2];
    let qz = quat[3];

    let r00 = 1.0 - 2.0 * (qy * qy + qz * qz);
    let r01 = 2.0 * (qx * qy - qw * qz);
    let r02 = 2.0 * (qx * qz + qw * qy);

    let r10 = 2.0 * (qx * qy + qw * qz);
    let r11 = 1.0 - 2.0 * (qx * qx + qz * qz);
    let r12 = 2.0 * (qy * qz - qw * qx);

    let r20 = 2.0 * (qx * qz - qw * qy);
    let r21 = 2.0 * (qy * qz + qw * qx);
    let r22 = 1.0 - 2.0 * (qx * qx + qy * qy);

    Array2::from_shape_vec((3, 3), vec![r00, r01, r02, r10, r11, r12, r20, r21, r22]).unwrap()
}
