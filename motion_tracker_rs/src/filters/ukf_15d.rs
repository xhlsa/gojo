use crate::types::linalg::*;
use nalgebra::{SMatrix, SVector, Cholesky};
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Ukf15dState {
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

pub struct Ukf15d {
    /// Time step [seconds]
    pub dt: f64,

    /// State vector [15D]
    pub state: StateVec15,

    /// Covariance matrix [15x15]
    pub covariance: StateMat15,

    /// Process noise matrix [15x15]
    pub process_noise: StateMat15,

    /// Unscented transform parameters
    #[allow(dead_code)]
    alpha: f64, // Spread of sigma points (typically 1e-3)
    #[allow(dead_code)]
    beta: f64,  // Prior knowledge (2.0 for Gaussian)
    #[allow(dead_code)]
    kappa: f64, // Secondary scaling (0.0 or 3-n)
    lambda: f64, // Combined scaling parameter

    /// Weights for sigma points (31-element vector)
    weights_mean: SigmaWeights,
    weights_cov: SigmaWeights,

    /// Origin for local frame (lat, lon)
    origin: Option<(f64, f64)>,

    /// Update counters
    gps_updates: u64,
    accel_updates: u64,
    gyro_updates: u64,

    /// Track if filter has been initialized from first GPS fix (First Fix Snap strategy)
    is_initialized: bool,
}

impl Ukf15d {
    /// Create a new 15D UKF
    pub fn new(dt: f64, _gps_noise_std: f64, accel_noise_std: f64, gyro_noise_std: f64) -> Self {
        let mut state = StateVec15::zeros();
        // Initialize quaternion to identity
        state[6] = 1.0;

        // Initialize covariance (15x15) - same as EKF
        let mut covariance = StateMat15::zeros();
        let diag = [
            100.0, 100.0, 100.0, // position: 100 m² uncertainty
            10.0, 10.0, 10.0, // velocity: 10 m²/s² uncertainty
            1.0, 1.0, 1.0, 1.0, // quaternion: 1.0 (unitless)
            0.1, 0.1, 0.1, // gyro bias: 0.1 rad²/s²
            0.1, 0.1, // accel bias (x, y): assume stable sensors at start
        ];
        for (i, &val) in diag.iter().enumerate() {
            covariance[(i, i)] = val;
        }

        // Process noise matrix - same as EKF
        let mut process_noise = StateMat15::zeros();
        let accel_var = accel_noise_std * accel_noise_std;
        let gyro_var = gyro_noise_std * gyro_noise_std;

        // Position: constant velocity model
        let q_pos = (1000.0 * 0.25 * dt.powi(4) * accel_var).max(1e-5);
        for i in 0..3 {
            process_noise[(i, i)] = q_pos;
        }

        // Velocity: driven by accel noise
        let q_vel = 1.0;
        for i in 3..6 {
            process_noise[(i, i)] = q_vel;
        }

        // Quaternion: stable (integrated from gyro)
        for i in 6..10 {
            process_noise[(i, i)] = gyro_var * dt * dt;
        }

        // Gyro bias: random walk (locked down)
        let q_gyro_bias = 1e-8;
        for i in 10..13 {
            process_noise[(i, i)] = q_gyro_bias;
        }

        // Accel bias: random walk (locked down)
        let q_accel_bias = 1e-8;
        for i in 13..15 {
            process_noise[(i, i)] = q_accel_bias;
        }

        // Unscented transform parameters
        let alpha = 1e-3; // Spread of sigma points
        let beta = 2.0; // Gaussian prior
        let kappa = 0.0; // Secondary scaling
        let lambda = alpha * alpha * (STATE_DIM_15 as f64 + kappa) - STATE_DIM_15 as f64;

        // Compute weights
        let mut weights_mean = SigmaWeights::zeros();
        let mut weights_cov = SigmaWeights::zeros();

        weights_mean[0] = lambda / (STATE_DIM_15 as f64 + lambda);
        weights_cov[0] = lambda / (STATE_DIM_15 as f64 + lambda) + (1.0 - alpha * alpha + beta);

        for i in 1..SIGMA_COUNT_15 {
            weights_mean[i] = 1.0 / (2.0 * (STATE_DIM_15 as f64 + lambda));
            weights_cov[i] = 1.0 / (2.0 * (STATE_DIM_15 as f64 + lambda));
        }

        Self {
            dt,
            state,
            covariance,
            process_noise,
            alpha,
            beta,
            kappa,
            lambda,
            weights_mean,
            weights_cov,
            origin: None,
            gps_updates: 0,
            accel_updates: 0,
            gyro_updates: 0,
            is_initialized: false,
        }
    }

    /// Get current state
    pub fn get_state(&self) -> Ukf15dState {
        let trace = (0..STATE_DIM_15).map(|i| self.covariance[(i, i)]).sum();
        Ukf15dState {
            position: (self.state[0], self.state[1], self.state[2]),
            velocity: (self.state[3], self.state[4], self.state[5]),
            quaternion: (self.state[6], self.state[7], self.state[8], self.state[9]),
            gyro_bias: (self.state[10], self.state[11], self.state[12]),
            accel_bias: (self.state[13], self.state[14], 0.0),
            covariance_trace: trace,
            gps_updates: self.gps_updates,
            accel_updates: self.accel_updates,
            gyro_updates: self.gyro_updates,
        }
    }

    /// Generate sigma points from current state and covariance
    fn generate_sigma_points(&self) -> SigmaPoints15 {
        let mut sigmas = [StateVec15::zeros(); SIGMA_COUNT_15];

        // Compute L = sqrt((n+lambda)*P) using Cholesky decomposition
        let scale = STATE_DIM_15 as f64 + self.lambda;
        let scaled_cov = self.covariance * scale;

        // Direct nalgebra Cholesky decomposition
        let l_mat = match Cholesky::new(scaled_cov) {
            Some(chol) => chol.l(),
            None => {
                eprintln!("[UKF] Cholesky decomposition failed, using identity for sigma spread");
                StateMat15::identity()
            }
        };

        // Sigma point 0: mean
        sigmas[0] = self.state;

        // Sigma points 1..n: mean + sqrt((n+lambda)*P)_col[i]
        for i in 0..STATE_DIM_15 {
            let offset: StateVec15 = l_mat.column(i).into();
            sigmas[i + 1] = self.state + offset;
        }

        // Sigma points (n+1)..(2n): mean - sqrt((n+lambda)*P)_col[i]
        for i in 0..STATE_DIM_15 {
            let offset: StateVec15 = l_mat.column(i).into();
            sigmas[i + 1 + STATE_DIM_15] = self.state - offset;
        }

        sigmas
    }

    /// Recombine sigma points via unscented transform
    fn recombine_sigma_points(&self, sigmas: &SigmaPoints15) -> (StateVec15, StateMat15) {
        // Weighted mean
        let mut x_pred = StateVec15::zeros();
        for i in 0..SIGMA_COUNT_15 {
            x_pred += sigmas[i] * self.weights_mean[i];
        }

        // Weighted covariance
        let mut p_pred = StateMat15::zeros();
        for i in 0..SIGMA_COUNT_15 {
            let diff = sigmas[i] - x_pred;
            p_pred += (diff * diff.transpose()) * self.weights_cov[i];
        }

        (x_pred, p_pred)
    }

    /// Predict step: propagate through motion model using unscented transform
    pub fn predict(&mut self, accel_raw: (f64, f64, f64), gyro_raw: (f64, f64, f64)) {
        // SECURITY CLAMP: same as EKF
        if self.dt > 0.5 {
            let scalar = self.dt / 0.02;
            let noise_growth = 1.0 * scalar;

            self.covariance[(0, 0)] += noise_growth;
            self.covariance[(1, 1)] += noise_growth;
            self.covariance[(2, 2)] += noise_growth;
            self.covariance[(3, 3)] += noise_growth;
            self.covariance[(4, 4)] += noise_growth;
            self.covariance[(5, 5)] += noise_growth;

            return;
        }

        // Update dynamic process noise (same as EKF)
        let speed = (self.state[3].powi(2) + self.state[4].powi(2) + self.state[5].powi(2)).sqrt();
        let accel_std = if speed < 2.0 {
            0.1
        } else if speed < 10.0 {
            0.1 + (speed - 2.0) * (1.4 / 8.0)
        } else {
            1.5
        };
        let accel_var_dyn = accel_std * accel_std;
        let q_pos_dyn = (1000.0 * 0.25 * self.dt.powi(4) * accel_var_dyn).max(1e-5);
        for i in 0..3 {
            self.process_noise[(i, i)] = q_pos_dyn;
        }

        // 1. Generate sigma points
        let sigmas = self.generate_sigma_points();

        // 2. Propagate through motion model (shared with EKF)
        // Pure nalgebra - no conversions needed!
        let mut predicted_sigmas = [StateVec15::zeros(); SIGMA_COUNT_15];
        for (idx, sigma) in sigmas.iter().enumerate() {
            predicted_sigmas[idx] = super::ekf_15d::motion_model(&sigma, accel_raw, gyro_raw, self.dt);
        }

        // 3. Recombine (unscented transform)
        let (x_pred, p_pred) = self.recombine_sigma_points(&predicted_sigmas);

        // 4. Update state and covariance
        self.state = x_pred;
        self.covariance = p_pred + self.process_noise;

        // 5. Re-normalize quaternion (states 6-9)
        let q_norm = (self.state[6].powi(2)
            + self.state[7].powi(2)
            + self.state[8].powi(2)
            + self.state[9].powi(2))
            .sqrt();
        if q_norm > 1e-6 {
            self.state[6] /= q_norm;
            self.state[7] /= q_norm;
            self.state[8] /= q_norm;
            self.state[9] /= q_norm;
        }

        // 6. Gravity well: adaptive Z-velocity damping for ground vehicles
        let accel_mag_xy = (accel_raw.0.powi(2) + accel_raw.1.powi(2)).sqrt();
        // Extract rotation matrix components from quaternion to check tilt
        let q0 = self.state[6];
        let q1 = self.state[7];
        let q2 = self.state[8];
        let q3 = self.state[9];
        let r20 = 2.0 * (q1 * q3 - q0 * q2);
        let r21 = 2.0 * (q2 * q3 + q0 * q1);
        let tilt_magnitude = (r20.powi(2) + r21.powi(2)).sqrt();

        // Aggressive damping when stable, softer during dynamics
        if accel_mag_xy < 2.0 && tilt_magnitude < 0.2 {
            self.state[5] *= 0.80;  // 20% decay when stable
        } else {
            self.state[5] *= 0.95;  // 5% decay during motion
        }

        // Exponential decay toward zero (10% per step)
        self.state[5] *= 0.90;

        // Hard limit at ±1 m/s (tighter for ground vehicles)
        self.state[5] = self.state[5].clamp(-1.0, 1.0);
    }

    /// Force symmetry of covariance matrix
    #[allow(dead_code)]
    fn enforce_covariance_symmetry(&mut self) {
        let p_t = self.covariance.transpose();
        self.covariance = (self.covariance + p_t) * 0.5;
    }

    /// Set state manually (for testing/initialization)
    pub fn set_state(
        &mut self,
        position: (f64, f64, f64),
        velocity: (f64, f64, f64),
        quaternion: (f64, f64, f64, f64),
        gyro_bias: (f64, f64, f64),
        accel_bias: (f64, f64),
    ) {
        self.state[0] = position.0;
        self.state[1] = position.1;
        self.state[2] = position.2;
        self.state[3] = velocity.0;
        self.state[4] = velocity.1;
        self.state[5] = velocity.2;

        // Normalize quaternion
        let q_norm = (quaternion.0 * quaternion.0
            + quaternion.1 * quaternion.1
            + quaternion.2 * quaternion.2
            + quaternion.3 * quaternion.3)
            .sqrt();
        if q_norm > 1e-9 {
            self.state[6] = quaternion.0 / q_norm;
            self.state[7] = quaternion.1 / q_norm;
            self.state[8] = quaternion.2 / q_norm;
            self.state[9] = quaternion.3 / q_norm;
        } else {
            self.state[6] = 1.0;
            self.state[7] = 0.0;
            self.state[8] = 0.0;
            self.state[9] = 0.0;
        }

        self.state[10] = gyro_bias.0;
        self.state[11] = gyro_bias.1;
        self.state[12] = gyro_bias.2;
        self.state[13] = accel_bias.0;
        self.state[14] = accel_bias.1;
    }

    /// Set local origin for GPS conversion and reset position
    pub fn set_origin(&mut self, lat: f64, lon: f64, _alt: f64) {
        // Store origin (lat, lon) for future lat/lon conversions
        self.origin = Some((lat, lon));
        // Reset position to local frame origin
        self.state[0] = 0.0;
        self.state[1] = 0.0;
        self.state[2] = 0.0;
    }

    /// Initialize filter from first GPS fix (First Fix Snap strategy).
    ///
    /// Snaps position to GPS fix and initializes covariance based on GPS accuracy.
    /// This eliminates velocity spikes and covariance explosions on startup.
    /// Must be called after set_origin().
    pub fn initialize_from_gps(&mut self, _lat: f64, _lon: f64, _alt: f64, accuracy: f64) {
        // Position already set to origin by set_origin()
        // (state[0], state[1], state[2] = 0.0 in local frame)

        // Velocity = 0 (assume stationary start)
        self.state[3] = 0.0;
        self.state[4] = 0.0;
        self.state[5] = 0.0;

        // Quaternion = identity (level platform)
        self.state[6] = 1.0;
        self.state[7] = 0.0;
        self.state[8] = 0.0;
        self.state[9] = 0.0;

        // Biases = 0 (will be estimated during operation)
        for i in 10..15 {
            self.state[i] = 0.0;
        }

        // Tighten covariance based on GPS accuracy
        let gps_var = (accuracy * accuracy).max(9.0);  // Min 3m std dev (9m²)

        // Position: GPS accuracy
        self.covariance[(0, 0)] = gps_var;
        self.covariance[(1, 1)] = gps_var;
        self.covariance[(2, 2)] = gps_var * 4.0;  // Altitude uncertainty worse

        // Velocity: assume stationary (0.1 m/s uncertainty = 0.01 m²/s²)
        for i in 3..6 {
            self.covariance[(i, i)] = 0.01;
        }

        // Quaternion: moderate uncertainty (don't know heading yet)
        for i in 6..10 {
            self.covariance[(i, i)] = 0.1;
        }

        // Gyro bias: same as constructor (will be learned)
        for i in 10..13 {
            self.covariance[(i, i)] = 0.1;
        }

        // Accel bias: same as constructor (will be learned)
        for i in 13..15 {
            self.covariance[(i, i)] = 0.1;
        }

        self.is_initialized = true;
    }

    /// Check if filter has been initialized from first GPS fix.
    pub fn is_initialized(&self) -> bool {
        self.is_initialized
    }

    /// Update UKF state with GPS position measurement using unscented transform
    pub fn update_gps(&mut self, gps_pos: (f64, f64, f64), accuracy: f64, _timestamp: f64) -> f64 {
        // Skip updates until filter has been initialized from first GPS fix
        if !self.is_initialized {
            return f64::INFINITY;
        }

        // Calculate speed for dynamic noise floor
        let vx = self.state[3];
        let vy = self.state[4];
        let vz = self.state[5];
        let speed = (vx * vx + vy * vy + vz * vz).sqrt();

        // Dynamic Floor: Trust Inertia at low speed, GPS at high speed
        let accuracy_floor = if speed < 3.0 {
            10.0
        } else if speed < 15.0 {
            10.0 - (speed - 3.0) * (7.0 / 12.0)
        } else {
            3.0
        };

        // Enforce GPS accuracy floor
        let safe_accuracy = accuracy.max(accuracy_floor);
        let gps_noise = safe_accuracy * safe_accuracy;

        // Convert lat/lon to local meters if origin is set
        let (mut pos_x, mut pos_y, pos_z) = gps_pos;
        if let Some((origin_lat, origin_lon)) = self.origin {
            let (x, y) = latlon_to_meters(pos_x, pos_y, origin_lat, origin_lon);
            pos_x = x;
            pos_y = y;
        }

        // 1. Generate sigma points
        let sigmas = self.generate_sigma_points();

        // 2. Transform sigma points to measurement space (extract position states 0,1,2)
        let mut z_sigmas = [SVector::<f64, 3>::zeros(); SIGMA_COUNT_15];
        for (idx, sigma) in sigmas.iter().enumerate() {
            // Measurement is just position: [x, y, z]
            z_sigmas[idx][0] = sigma[0];
            z_sigmas[idx][1] = sigma[1];
            z_sigmas[idx][2] = sigma[2];
        }

        // 3. Compute predicted measurement mean and covariance using unscented transform
        let mut z_pred = SVector::<f64, 3>::zeros();
        for i in 0..SIGMA_COUNT_15 {
            z_pred += z_sigmas[i] * self.weights_mean[i];
        }

        // Measurement covariance (predicted): Pzz = Σ w_cov[i] * (z_sigma[i] - z_pred)^T * (z_sigma[i] - z_pred)
        let mut p_zz = SMatrix::<f64, 3, 3>::zeros();
        for i in 0..SIGMA_COUNT_15 {
            let residual = z_sigmas[i] - z_pred;
            p_zz += (residual * residual.transpose()) * self.weights_cov[i];
        }

        // Add GPS measurement noise R (3x3)
        // Altitude has 2x worse accuracy (reduced from 4x for stronger altitude correction)
        let mut r_gps = SMatrix::<f64, 3, 3>::zeros();
        r_gps[(0, 0)] = gps_noise;
        r_gps[(1, 1)] = gps_noise;
        r_gps[(2, 2)] = gps_noise * 2.0;

        let p_zz_plus_r = p_zz + r_gps;

        // 4. Compute cross-covariance Pxy = Σ w_cov[i] * (sigma[i] - x_pred) * (z_sigma[i] - z_pred)^T
        let mut p_xy = SMatrix::<f64, 15, 3>::zeros();
        for i in 0..SIGMA_COUNT_15 {
            let x_residual = sigmas[i] - self.state;
            let z_residual = z_sigmas[i] - z_pred;
            p_xy += (x_residual * z_residual.transpose()) * self.weights_cov[i];
        }

        // 5. Compute Kalman gain K = Pxy * (Pzz + R)^-1
        let Some(p_zz_r_inv) = p_zz_plus_r.try_inverse() else {
            return f64::INFINITY; // Singular innovation covariance
        };

        let k = p_xy * p_zz_r_inv;

        // 6. Compute innovation and update state
        let mut innovation = SVector::<f64, 3>::zeros();
        innovation[0] = pos_x - z_pred[0];
        innovation[1] = pos_y - z_pred[1];
        innovation[2] = pos_z - z_pred[2];

        // Calculate NIS for filter consistency monitoring
        let nis = innovation.dot(&(p_zz_r_inv * innovation));

        let dx = k * innovation;
        for i in 0..STATE_DIM_15 {
            self.state[i] += dx[i];
        }

        // Re-normalize quaternion after update
        let q_norm = (self.state[6].powi(2)
            + self.state[7].powi(2)
            + self.state[8].powi(2)
            + self.state[9].powi(2))
            .sqrt();
        if q_norm > 1e-6 {
            self.state[6] /= q_norm;
            self.state[7] /= q_norm;
            self.state[8] /= q_norm;
            self.state[9] /= q_norm;
        }

        // 7. Update covariance: P = P - K * Pzz * K^T (Joseph form for stability)
        let k_p_zz = k * p_zz;
        self.covariance = self.covariance - k_p_zz * k.transpose();

        // Force symmetry and positive definiteness
        let p_t = self.covariance.transpose();
        self.covariance = (self.covariance + p_t) * 0.5;

        for i in 0..STATE_DIM_15 {
            if self.covariance[(i, i)] < 1e-9 {
                self.covariance[(i, i)] = 1e-9;
            }
        }

        self.gps_updates += 1;

        // Return NIS for consistency monitoring
        nis
    }

    /// Zero-velocity update on Z-axis (vertical velocity constraint for ground vehicles)
    ///
    /// Clamps vertical velocity to 0 and tightens Z-velocity covariance to enforce
    /// the ground-vehicle constraint. Prevents altitude drift during GPS denial.
    pub fn zero_vertical_velocity(&mut self, noise_std: f64) {
        // Directly clamp Z-velocity to 0
        self.state[5] = 0.0;

        // Tighten Z-velocity covariance (P[5,5])
        let var = noise_std * noise_std;
        self.covariance[(5, 5)] = var.max(1e-9);
    }

    /// Update UKF state with GPS velocity measurement
    pub fn update_gps_velocity(&mut self, speed: f64, bearing_rad: f64, speed_std: f64) {
        // Convert speed/bearing to ENU components (bearing: 0 = North, clockwise)
        let vx_meas = speed * bearing_rad.sin(); // East
        let vy_meas = speed * bearing_rad.cos(); // North
        let vz_meas = 0.0;

        let mut meas_vel = SVector::<f64, 3>::zeros();
        meas_vel[0] = vx_meas;
        meas_vel[1] = vy_meas;
        meas_vel[2] = vz_meas;

        // 1. Generate sigma points
        let sigmas = self.generate_sigma_points();

        // 2. Transform sigma points to measurement space (extract velocity states 3,4,5)
        let mut z_sigmas = [SVector::<f64, 3>::zeros(); SIGMA_COUNT_15];
        for (idx, sigma) in sigmas.iter().enumerate() {
            z_sigmas[idx][0] = sigma[3];
            z_sigmas[idx][1] = sigma[4];
            z_sigmas[idx][2] = sigma[5];
        }

        // 3. Compute predicted velocity mean and covariance
        let mut z_pred = SVector::<f64, 3>::zeros();
        for i in 0..SIGMA_COUNT_15 {
            z_pred += z_sigmas[i] * self.weights_mean[i];
        }

        // Measurement covariance (predicted)
        let mut p_zz = SMatrix::<f64, 3, 3>::zeros();
        for i in 0..SIGMA_COUNT_15 {
            let residual = z_sigmas[i] - z_pred;
            p_zz += (residual * residual.transpose()) * self.weights_cov[i];
        }

        // Add GPS velocity measurement noise R
        let mut r_gps = SMatrix::<f64, 3, 3>::zeros();
        let var = (speed_std * speed_std).max(0.0001);
        r_gps[(0, 0)] = var;
        r_gps[(1, 1)] = var;
        r_gps[(2, 2)] = var * 2.0; // slight damp on vertical

        let p_zz_plus_r = p_zz + r_gps;

        // 4. Compute cross-covariance Pxy
        let mut p_xy = SMatrix::<f64, 15, 3>::zeros();
        for i in 0..SIGMA_COUNT_15 {
            let x_residual = sigmas[i] - self.state;
            let z_residual = z_sigmas[i] - z_pred;
            p_xy += (x_residual * z_residual.transpose()) * self.weights_cov[i];
        }

        // 5. Compute Kalman gain
        if let Some(p_zz_r_inv) = p_zz_plus_r.try_inverse() {
            let k = p_xy * p_zz_r_inv;

            // 6. Compute innovation
            let innovation = meas_vel - z_pred;

            // Clamp extreme innovations
            let mut innovation_clamped = innovation;
            for i in 0..3 {
                innovation_clamped[i] = innovation_clamped[i].clamp(-50.0, 50.0);
            }

            // 7. Update state
            let dx = k * innovation_clamped;
            for i in 0..STATE_DIM_15 {
                self.state[i] += dx[i];
            }

            // 8. Update covariance
            let k_p_zz = k * p_zz;
            self.covariance = self.covariance - k_p_zz * k.transpose();

            // Force symmetry
            let p_t = self.covariance.transpose();
            self.covariance = (self.covariance + p_t) * 0.5;
        }
    }
}

/// Convert lat/lon to local meters using equirectangular approximation
fn latlon_to_meters(lat: f64, lon: f64, origin_lat: f64, origin_lon: f64) -> (f64, f64) {
    const R: f64 = 6_371_000.0;
    let d_lat = (lat - origin_lat).to_radians();
    let d_lon = (lon - origin_lon).to_radians();
    let x = R * d_lon * origin_lat.to_radians().cos();
    let y = R * d_lat;
    (x, y)
}
