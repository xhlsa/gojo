use ndarray::{Array1, Array2};
use serde::{Deserialize, Serialize};

const STATE_DIM: usize = 15;
const SIGMA_COUNT: usize = 2 * STATE_DIM + 1; // 31 sigma points

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
    pub state: Array1<f64>,

    /// Covariance matrix [15x15]
    pub covariance: Array2<f64>,

    /// Process noise matrix [15x15]
    pub process_noise: Array2<f64>,

    /// Unscented transform parameters
    #[allow(dead_code)]
    alpha: f64, // Spread of sigma points (typically 1e-3)
    #[allow(dead_code)]
    beta: f64,  // Prior knowledge (2.0 for Gaussian)
    #[allow(dead_code)]
    kappa: f64, // Secondary scaling (0.0 or 3-n)
    lambda: f64, // Combined scaling parameter

    /// Weights for sigma points
    weights_mean: Array1<f64>,
    weights_cov: Array1<f64>,

    /// Origin for local frame (lat, lon)
    origin: Option<(f64, f64)>,

    /// Update counters
    gps_updates: u64,
    accel_updates: u64,
    gyro_updates: u64,
}

impl Ukf15d {
    /// Create a new 15D UKF
    pub fn new(dt: f64, _gps_noise_std: f64, accel_noise_std: f64, gyro_noise_std: f64) -> Self {
        let mut state = Array1::<f64>::zeros(15);
        // Initialize quaternion to identity
        state[6] = 1.0;

        // Initialize covariance (15x15) - same as EKF
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

        // Process noise matrix - same as EKF
        let mut process_noise = Array2::<f64>::zeros((15, 15));
        let accel_var = accel_noise_std * accel_noise_std;
        let gyro_var = gyro_noise_std * gyro_noise_std;

        // Position: constant velocity model
        let q_pos = (1000.0 * 0.25 * dt.powi(4) * accel_var).max(1e-5);
        for i in 0..3 {
            process_noise[[i, i]] = q_pos;
        }

        // Velocity: driven by accel noise
        let q_vel = 1.0;
        for i in 3..6 {
            process_noise[[i, i]] = q_vel;
        }

        // Quaternion: stable (integrated from gyro)
        for i in 6..10 {
            process_noise[[i, i]] = gyro_var * dt * dt;
        }

        // Gyro bias: random walk (locked down)
        let q_gyro_bias = 1e-8;
        for i in 10..13 {
            process_noise[[i, i]] = q_gyro_bias;
        }

        // Accel bias: random walk (locked down)
        let q_accel_bias = 1e-8;
        for i in 13..15 {
            process_noise[[i, i]] = q_accel_bias;
        }

        // Unscented transform parameters
        let alpha = 1e-3; // Spread of sigma points
        let beta = 2.0; // Gaussian prior
        let kappa = 0.0; // Secondary scaling
        let lambda = alpha * alpha * (STATE_DIM as f64 + kappa) - STATE_DIM as f64;

        // Compute weights
        let mut weights_mean = Array1::<f64>::zeros(SIGMA_COUNT);
        let mut weights_cov = Array1::<f64>::zeros(SIGMA_COUNT);

        weights_mean[0] = lambda / (STATE_DIM as f64 + lambda);
        weights_cov[0] = lambda / (STATE_DIM as f64 + lambda) + (1.0 - alpha * alpha + beta);

        for i in 1..SIGMA_COUNT {
            weights_mean[i] = 1.0 / (2.0 * (STATE_DIM as f64 + lambda));
            weights_cov[i] = 1.0 / (2.0 * (STATE_DIM as f64 + lambda));
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
        }
    }

    /// Get current state
    pub fn get_state(&self) -> Ukf15dState {
        Ukf15dState {
            position: (self.state[0], self.state[1], self.state[2]),
            velocity: (self.state[3], self.state[4], self.state[5]),
            quaternion: (self.state[6], self.state[7], self.state[8], self.state[9]),
            gyro_bias: (self.state[10], self.state[11], self.state[12]),
            accel_bias: (self.state[13], self.state[14], 0.0),
            covariance_trace: self.covariance.diag().sum(),
            gps_updates: self.gps_updates,
            accel_updates: self.accel_updates,
            gyro_updates: self.gyro_updates,
        }
    }

    /// Generate sigma points from current state and covariance
    fn generate_sigma_points(&self) -> Vec<Array1<f64>> {
        let mut sigmas = Vec::with_capacity(SIGMA_COUNT);

        // Compute L = sqrt((n+lambda)*P) using Cholesky decomposition
        let scale = self.state.len() as f64 + self.lambda;
        let scaled_cov = &self.covariance * scale;

        // Compute Cholesky decomposition: P = L * L^T
        let l_mat = match scaled_cov.view().into_shape((STATE_DIM, STATE_DIM)) {
            Ok(mat) => {
                let na_mat = nalgebra::DMatrix::from_row_slice(STATE_DIM, STATE_DIM, mat.as_slice().unwrap());
                match na_mat.cholesky() {
                    Some(chol) => chol.l().to_owned(),
                    None => {
                        eprintln!("[UKF] Cholesky decomposition failed, using zero sigma points");
                        nalgebra::DMatrix::zeros(STATE_DIM, STATE_DIM)
                    }
                }
            }
            Err(_) => {
                eprintln!("[UKF] Covariance reshape failed");
                nalgebra::DMatrix::zeros(STATE_DIM, STATE_DIM)
            }
        };

        // Sigma point 0: mean
        sigmas.push(self.state.clone());

        // Sigma points 1..n: mean + sqrt((n+lambda)*P)_col[i]
        for i in 0..STATE_DIM {
            let mut sigma = self.state.clone();
            for j in 0..STATE_DIM {
                sigma[j] += l_mat[(j, i)];
            }
            sigmas.push(sigma);
        }

        // Sigma points (n+1)..(2n): mean - sqrt((n+lambda)*P)_col[i-n]
        for i in 0..STATE_DIM {
            let mut sigma = self.state.clone();
            for j in 0..STATE_DIM {
                sigma[j] -= l_mat[(j, i)];
            }
            sigmas.push(sigma);
        }

        sigmas
    }

    /// Recombine sigma points via unscented transform
    fn recombine_sigma_points(&self, sigmas: &[Array1<f64>]) -> (Array1<f64>, Array2<f64>) {
        // Weighted mean
        let mut x_pred = Array1::<f64>::zeros(STATE_DIM);
        for (i, sigma) in sigmas.iter().enumerate() {
            x_pred = &x_pred + &(sigma * self.weights_mean[i]);
        }

        // Weighted covariance
        let mut p_pred = Array2::<f64>::zeros((STATE_DIM, STATE_DIM));
        for (i, sigma) in sigmas.iter().enumerate() {
            let residual = sigma - &x_pred;
            let outer = residual.view().into_shape((STATE_DIM, 1))
                .map(|r| r.dot(&r.t()))
                .unwrap_or_else(|_| Array2::zeros((STATE_DIM, STATE_DIM)));
            p_pred = &p_pred + &(outer * self.weights_cov[i]);
        }

        (x_pred, p_pred)
    }

    /// Predict step: propagate through motion model using unscented transform
    pub fn predict(&mut self, accel_raw: (f64, f64, f64), gyro_raw: (f64, f64, f64)) {
        // SECURITY CLAMP: same as EKF
        if self.dt > 0.5 {
            let scalar = self.dt / 0.02;
            let noise_growth = 1.0 * scalar;

            self.covariance[[0, 0]] += noise_growth;
            self.covariance[[1, 1]] += noise_growth;
            self.covariance[[2, 2]] += noise_growth;
            self.covariance[[3, 3]] += noise_growth;
            self.covariance[[4, 4]] += noise_growth;
            self.covariance[[5, 5]] += noise_growth;

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
            self.process_noise[[i, i]] = q_pos_dyn;
        }

        // 1. Generate sigma points
        let sigmas = self.generate_sigma_points();

        // 2. Propagate through motion model (shared with EKF)
        let mut predicted_sigmas = Vec::with_capacity(SIGMA_COUNT);
        for sigma in &sigmas {
            let predicted = super::ekf_15d::motion_model(sigma, accel_raw, gyro_raw, self.dt);
            predicted_sigmas.push(predicted);
        }

        // 3. Recombine (unscented transform)
        let (x_pred, p_pred) = self.recombine_sigma_points(&predicted_sigmas);

        // 4. Update state and covariance
        self.state = x_pred;
        self.covariance = p_pred + &self.process_noise;

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

        // Hard limit at ±5 m/s
        self.state[5] = self.state[5].clamp(-5.0, 5.0);
    }

    /// Force symmetry of covariance matrix
    #[allow(dead_code)]
    fn enforce_covariance_symmetry(&mut self) {
        let p_t = self.covariance.t();
        self.covariance = (&self.covariance + &p_t) * 0.5;
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

    /// Update UKF state with GPS position measurement using unscented transform
    pub fn update_gps(&mut self, gps_pos: (f64, f64, f64), accuracy: f64, _timestamp: f64) -> f64 {
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
        let mut z_sigmas = Vec::with_capacity(SIGMA_COUNT);
        for sigma in &sigmas {
            // Measurement is just position: [x, y, z]
            z_sigmas.push(Array1::<f64>::from_vec(vec![sigma[0], sigma[1], sigma[2]]));
        }

        // 3. Compute predicted measurement mean and covariance using unscented transform
        let mut z_pred = Array1::<f64>::zeros(3);
        for (i, z_sigma) in z_sigmas.iter().enumerate() {
            z_pred = &z_pred + &(z_sigma * self.weights_mean[i]);
        }

        // Measurement covariance (predicted): Pzz = Σ w_cov[i] * (z_sigma[i] - z_pred)^T * (z_sigma[i] - z_pred)
        let mut p_zz = Array2::<f64>::zeros((3, 3));
        for (i, z_sigma) in z_sigmas.iter().enumerate() {
            let residual = z_sigma - &z_pred;
            let outer = residual.view().into_shape((3, 1))
                .map(|r| r.dot(&r.t()))
                .unwrap_or_else(|_| Array2::zeros((3, 3)));
            p_zz = &p_zz + &(outer * self.weights_cov[i]);
        }

        // Add GPS measurement noise R (3x3)
        // Altitude has 2x worse accuracy (reduced from 4x for stronger altitude correction)
        let mut r_gps = Array2::<f64>::zeros((3, 3));
        r_gps[[0, 0]] = gps_noise;
        r_gps[[1, 1]] = gps_noise;
        r_gps[[2, 2]] = gps_noise * 2.0;

        let p_zz_plus_r = &p_zz + &r_gps;

        // 4. Compute cross-covariance Pxy = Σ w_cov[i] * (sigma[i] - x_pred) * (z_sigma[i] - z_pred)^T
        let mut p_xy = Array2::<f64>::zeros((STATE_DIM, 3));
        for (i, sigma) in sigmas.iter().enumerate() {
            let x_residual = sigma - &self.state;
            let z_residual = &z_sigmas[i] - &z_pred;
            let outer = x_residual.view().into_shape((STATE_DIM, 1))
                .map(|xr| xr.dot(&z_residual.view().into_shape((1, 3)).unwrap()))
                .unwrap_or_else(|_| Array2::zeros((STATE_DIM, 3)));
            p_xy = &p_xy + &(outer * self.weights_cov[i]);
        }

        // 5. Compute Kalman gain K = Pxy * (Pzz + R)^-1
        use nalgebra::Matrix3;
        let p_zz_r_mat = Matrix3::new(
            p_zz_plus_r[[0, 0]], p_zz_plus_r[[0, 1]], p_zz_plus_r[[0, 2]],
            p_zz_plus_r[[1, 0]], p_zz_plus_r[[1, 1]], p_zz_plus_r[[1, 2]],
            p_zz_plus_r[[2, 0]], p_zz_plus_r[[2, 1]], p_zz_plus_r[[2, 2]],
        );

        let Some(p_zz_r_inv_na) = p_zz_r_mat.try_inverse() else {
            return f64::INFINITY; // Singular innovation covariance
        };

        let mut p_zz_r_inv = Array2::<f64>::zeros((3, 3));
        for i in 0..3 {
            for j in 0..3 {
                p_zz_r_inv[[i, j]] = p_zz_r_inv_na[(i, j)];
            }
        }

        let k = p_xy.dot(&p_zz_r_inv);

        // 6. Compute innovation and update state
        let innovation = Array1::<f64>::from_vec(vec![
            pos_x - z_pred[0],
            pos_y - z_pred[1],
            pos_z - z_pred[2],
        ]);

        // Calculate NIS for filter consistency monitoring
        let nis = {
            let temp = p_zz_r_inv.dot(&innovation);
            innovation.dot(&temp)
        };

        let dx = k.dot(&innovation);
        for i in 0..STATE_DIM {
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
        let k_p_zz = k.dot(&p_zz);
        self.covariance = &self.covariance - &k_p_zz.dot(&k.t());

        // Force symmetry and positive definiteness
        let p_t = self.covariance.t();
        self.covariance = (&self.covariance + &p_t) * 0.5;

        for i in 0..STATE_DIM {
            if self.covariance[[i, i]] < 1e-9 {
                self.covariance[[i, i]] = 1e-9;
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
        self.covariance[[5, 5]] = var.max(1e-9);
    }

    /// Update UKF state with GPS velocity measurement
    pub fn update_gps_velocity(&mut self, speed: f64, bearing_rad: f64, speed_std: f64) {
        // Convert speed/bearing to ENU components (bearing: 0 = North, clockwise)
        let vx_meas = speed * bearing_rad.sin(); // East
        let vy_meas = speed * bearing_rad.cos(); // North
        let vz_meas = 0.0;

        let meas_vel = Array1::<f64>::from_vec(vec![vx_meas, vy_meas, vz_meas]);

        // 1. Generate sigma points
        let sigmas = self.generate_sigma_points();

        // 2. Transform sigma points to measurement space (extract velocity states 3,4,5)
        let mut z_sigmas = Vec::with_capacity(SIGMA_COUNT);
        for sigma in &sigmas {
            z_sigmas.push(Array1::<f64>::from_vec(vec![sigma[3], sigma[4], sigma[5]]));
        }

        // 3. Compute predicted velocity mean and covariance
        let mut z_pred = Array1::<f64>::zeros(3);
        for (i, z_sigma) in z_sigmas.iter().enumerate() {
            z_pred = &z_pred + &(z_sigma * self.weights_mean[i]);
        }

        // Measurement covariance (predicted)
        let mut p_zz = Array2::<f64>::zeros((3, 3));
        for (i, z_sigma) in z_sigmas.iter().enumerate() {
            let residual = z_sigma - &z_pred;
            let outer = residual.view().into_shape((3, 1))
                .map(|r| r.dot(&r.t()))
                .unwrap_or_else(|_| Array2::zeros((3, 3)));
            p_zz = &p_zz + &(outer * self.weights_cov[i]);
        }

        // Add GPS velocity measurement noise R
        let mut r_gps = Array2::<f64>::zeros((3, 3));
        let var = (speed_std * speed_std).max(0.0001);
        r_gps[[0, 0]] = var;
        r_gps[[1, 1]] = var;
        r_gps[[2, 2]] = var * 2.0; // slight damp on vertical

        let p_zz_plus_r = &p_zz + &r_gps;

        // 4. Compute cross-covariance Pxy
        let mut p_xy = Array2::<f64>::zeros((STATE_DIM, 3));
        for (i, sigma) in sigmas.iter().enumerate() {
            let x_residual = sigma - &self.state;
            let z_residual = &z_sigmas[i] - &z_pred;
            let outer = x_residual.view().into_shape((STATE_DIM, 1))
                .map(|xr| xr.dot(&z_residual.view().into_shape((1, 3)).unwrap()))
                .unwrap_or_else(|_| Array2::zeros((STATE_DIM, 3)));
            p_xy = &p_xy + &(outer * self.weights_cov[i]);
        }

        // 5. Compute Kalman gain
        use nalgebra::Matrix3;
        let p_zz_r_mat = Matrix3::new(
            p_zz_plus_r[[0, 0]], p_zz_plus_r[[0, 1]], p_zz_plus_r[[0, 2]],
            p_zz_plus_r[[1, 0]], p_zz_plus_r[[1, 1]], p_zz_plus_r[[1, 2]],
            p_zz_plus_r[[2, 0]], p_zz_plus_r[[2, 1]], p_zz_plus_r[[2, 2]],
        );

        if let Some(p_zz_r_inv_na) = p_zz_r_mat.try_inverse() {
            let mut p_zz_r_inv = Array2::<f64>::zeros((3, 3));
            for i in 0..3 {
                for j in 0..3 {
                    p_zz_r_inv[[i, j]] = p_zz_r_inv_na[(i, j)];
                }
            }

            let k = p_xy.dot(&p_zz_r_inv);

            // 6. Compute innovation
            let innovation = &meas_vel - &z_pred;

            // Clamp extreme innovations
            let mut innovation_clamped = innovation.clone();
            for i in 0..3 {
                innovation_clamped[i] = innovation_clamped[i].clamp(-50.0, 50.0);
            }

            // 7. Update state
            let dx = k.dot(&innovation_clamped);
            for i in 0..STATE_DIM {
                self.state[i] += dx[i];
            }

            // 8. Update covariance
            let k_p_zz = k.dot(&p_zz);
            self.covariance = &self.covariance - &k_p_zz.dot(&k.t());

            // Force symmetry
            let p_t = self.covariance.t();
            self.covariance = (&self.covariance + &p_t) * 0.5;
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
