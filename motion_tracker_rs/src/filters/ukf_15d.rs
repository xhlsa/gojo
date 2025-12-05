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
}
