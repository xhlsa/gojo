use nalgebra::{Matrix3, SMatrix};
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

/// Predicted trajectory point for visualization/analysis
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct TrajectoryPoint {
    pub time: f64,
    pub position: (f64, f64, f64),
    pub velocity: (f64, f64, f64),
    pub quaternion: (f64, f64, f64, f64),
    /// Position covariance block (3x3 matrix flattened to 9 elements)
    /// Layout: [Pxx, Pxy, Pxz, Pyx, Pyy, Pyz, Pzx, Pzy, Pzz]
    /// Units: m² (uncertainty in position estimates)
    pub covariance_pos: [f64; 9],
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

    /// Last accepted GPS fix (time, position) for snap velocity inference
    last_gps_time: Option<f64>,
    last_gps_pos: Option<(f64, f64, f64)>,
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
        // Apply a strong multiplier to counter tiny dt^4 and add a floor so S isn't microscopic
        let q_pos = (1000.0 * 0.25 * dt.powi(4) * accel_var).max(1e-5); // minimal floor; let inertia dominate at low speed
        for i in 0..3 {
            process_noise[[i, i]] = q_pos;
        }

        // Velocity: driven by accel noise
        // Velocity process noise (tuned for responsiveness after ZUPT)
        let q_vel = 1.0;
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
            last_gps_time: None,
            last_gps_pos: None,
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
            self.state[6] = 1.0; // Default to identity
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

    /// Predict step: integrate kinematics with bias correction
    pub fn predict(&mut self, accel_raw: (f64, f64, f64), gyro_raw: (f64, f64, f64)) {
        // SECURITY CLAMP:
        // If dt is larger than 0.5s, the IMU data is mathematically useless for integration.
        // We are effectively "lost" in time.
        if self.dt > 0.5 {
            // Do NOT integrate. 
            // Just increase uncertainty (Q) to reflect the gap.
            let scalar = self.dt / 0.02; // How many "frames" did we miss?
            
            // Inflate P to admit we have no idea what happened during the blackout
            // (This prepares the filter to accept the next GPS point gracefully)
            // Using 1.0 as a base process noise scalar per frame (conservative)
            let noise_growth = 1.0 * scalar;

            self.covariance[[0, 0]] += noise_growth;
            self.covariance[[1, 1]] += noise_growth;
            self.covariance[[2, 2]] += noise_growth;
            self.covariance[[3, 3]] += noise_growth;
            self.covariance[[4, 4]] += noise_growth;
            self.covariance[[5, 5]] += noise_growth;
            
            return; 
        }

        // Dynamic process noise: very stiff at low speed, modest at highway speeds
        let speed = (self.state[3].powi(2) + self.state[4].powi(2) + self.state[5].powi(2)).sqrt();
        let accel_std = if speed < 2.0 {
            0.1
        } else if speed < 10.0 {
            0.1 + (speed - 2.0) * (1.4 / 8.0) // ramps to 1.5 at 10 m/s
        } else {
            1.5
        };
        let accel_var_dyn = accel_std * accel_std;
        let q_pos_dyn = (1000.0 * 0.25 * self.dt.powi(4) * accel_var_dyn).max(1e-5);
        for i in 0..3 {
            self.process_noise[[i, i]] = q_pos_dyn;
        }

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

    /// Multi-step trajectory prediction with proper second-order integration
    ///
    /// Uses current state and constant IMU readings to project future path.
    /// Applies soft Z-constraint to prevent altitude drift from unestimated Z-bias.
    /// Supports exponential acceleration decay for realistic braking/deceleration scenarios.
    ///
    /// # Arguments
    /// * `accel_raw` - Body frame acceleration (base value, decays exponentially if decay_rate > 0)
    /// * `gyro_raw` - Body frame angular velocity (will be held constant)
    /// * `horizon_sec` - Total prediction time [seconds]
    /// * `num_steps` - Number of prediction steps (resolution)
    /// * `apply_z_constraint` - Apply soft Z-velocity damping for ground vehicles
    /// * `accel_decay_rate` - Exponential decay rate for acceleration (1/s). 0.0 = constant, 0.5 = rapid decay
    ///
    /// # Returns
    /// Vector of predicted states at each time step (does NOT modify filter state)
    pub fn predict_trajectory(
        &self,
        accel_raw: (f64, f64, f64),
        gyro_raw: (f64, f64, f64),
        horizon_sec: f64,
        num_steps: usize,
        apply_z_constraint: bool,
        accel_decay_rate: f64,
    ) -> Vec<TrajectoryPoint> {
        if num_steps == 0 || horizon_sec <= 0.0 {
            return vec![];
        }

        let dt = horizon_sec / (num_steps as f64);
        let mut trajectory = Vec::with_capacity(num_steps);

        // Copy current state (don't modify filter)
        let gyro_bias = [self.state[10], self.state[11], self.state[12]];
        let accel_bias = [self.state[13], self.state[14], 0.0]; // Z-bias clamped to 0

        // Correct measurements (constant for entire horizon, but accel decays exponentially)
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

        // Initialize from current state
        let mut pos = [self.state[0], self.state[1], self.state[2]];
        let mut vel = [self.state[3], self.state[4], self.state[5]];
        let mut quat = [self.state[6], self.state[7], self.state[8], self.state[9]];

        // Initialize position covariance from current filter covariance (3x3 block)
        let mut p_pos = [
            self.covariance[[0, 0]], self.covariance[[0, 1]], self.covariance[[0, 2]],
            self.covariance[[1, 0]], self.covariance[[1, 1]], self.covariance[[1, 2]],
            self.covariance[[2, 0]], self.covariance[[2, 1]], self.covariance[[2, 2]],
        ];

        // Extract position process noise (3x3 block) for propagation
        let q_pos_block = [
            self.process_noise[[0, 0]], self.process_noise[[0, 1]], self.process_noise[[0, 2]],
            self.process_noise[[1, 0]], self.process_noise[[1, 1]], self.process_noise[[1, 2]],
            self.process_noise[[2, 0]], self.process_noise[[2, 1]], self.process_noise[[2, 2]],
        ];

        // Initialize acceleration with exponential decay support
        let mut accel_current = accel_corr;

        // Simulate forward
        for i in 0..num_steps {
            // === 1. Attitude Propagation (Quaternion Integration) ===
            let gyro_mag = (gyro_corr[0] * gyro_corr[0]
                + gyro_corr[1] * gyro_corr[1]
                + gyro_corr[2] * gyro_corr[2])
                .sqrt();

            if gyro_mag > 1e-6 {
                let half_angle = 0.5 * gyro_mag * dt;
                let scale = half_angle.sin() / gyro_mag;

                let dq = [
                    half_angle.cos(),
                    gyro_corr[0] * scale,
                    gyro_corr[1] * scale,
                    gyro_corr[2] * scale,
                ];

                // Quaternion multiplication: q_next = dq * q
                let qw = dq[0] * quat[0] - dq[1] * quat[1] - dq[2] * quat[2] - dq[3] * quat[3];
                let qx = dq[0] * quat[1] + dq[1] * quat[0] + dq[2] * quat[3] - dq[3] * quat[2];
                let qy = dq[0] * quat[2] - dq[1] * quat[3] + dq[2] * quat[0] + dq[3] * quat[1];
                let qz = dq[0] * quat[3] + dq[1] * quat[2] - dq[2] * quat[1] + dq[3] * quat[0];

                quat = [qw, qx, qy, qz];

                // Normalize quaternion
                let quat_mag = (quat[0] * quat[0]
                    + quat[1] * quat[1]
                    + quat[2] * quat[2]
                    + quat[3] * quat[3])
                    .sqrt();
                if quat_mag > 1e-6 {
                    quat[0] /= quat_mag;
                    quat[1] /= quat_mag;
                    quat[2] /= quat_mag;
                    quat[3] /= quat_mag;
                }
            }

            // === 2. Rotate Accel to World Frame (with exponential decay) ===
            let accel_world = rotate_accel_to_world(&quat, &accel_current);

            // Subtract gravity (world frame Z-axis)
            let linear_accel = [
                accel_world[0],
                accel_world[1],
                accel_world[2] - G,
            ];

            // === 3. Second-Order Position Integration (Gemini recommendation) ===
            // p_next = p + v * dt + 0.5 * a * dt^2
            pos[0] += vel[0] * dt + 0.5 * linear_accel[0] * dt * dt;
            pos[1] += vel[1] * dt + 0.5 * linear_accel[1] * dt * dt;
            pos[2] += vel[2] * dt + 0.5 * linear_accel[2] * dt * dt;

            // === 4. Velocity Integration ===
            // v_next = v + a * dt
            vel[0] += linear_accel[0] * dt;
            vel[1] += linear_accel[1] * dt;
            vel[2] += linear_accel[2] * dt;

            // === 4b. Exponential Acceleration Decay (for braking/deceleration) ===
            // Apply: accel(t) = accel(0) * exp(-decay_rate * t)
            if accel_decay_rate > 1e-6 {
                let time_elapsed = (i + 1) as f64 * dt;
                let decay_factor = (-accel_decay_rate * time_elapsed).exp();
                accel_current[0] = accel_corr[0] * decay_factor;
                accel_current[1] = accel_corr[1] * decay_factor;
                accel_current[2] = accel_corr[2] * decay_factor;
            }

            // === 5. "Gravity Well" Z-Constraint (Ground Vehicle Assumption) ===
            if apply_z_constraint {
                // Calculate lateral acceleration magnitude
                let accel_mag_xy = (linear_accel[0] * linear_accel[0]
                    + linear_accel[1] * linear_accel[1])
                    .sqrt();

                // Extract tilt angle from quaternion (roll/pitch deviation from level)
                // Simplified: check if quaternion is close to identity (level orientation)
                let qw = quat[0];
                let tilt_magnitude = (1.0 - qw.abs()).acos(); // Deviation from identity

                let vz_mag = vel[2].abs();

                // "Gravity Well": Aggressive damping when stable, soft during dynamics
                if accel_mag_xy < 2.0 && tilt_magnitude < 0.2 {
                    // Stable conditions: hard decay (kill 20% of Z-velocity per step)
                    vel[2] *= 0.80;

                    // Optional: Spring force pulling Z-position back to initial altitude
                    // Uncomment if you trust initial Z calibration:
                    // let initial_z = self.state[2];
                    // pos[2] += (initial_z - pos[2]) * 0.05;
                } else {
                    // Dynamic motion (speed bump, hill): soft 5% decay
                    vel[2] *= 0.95;
                }

                // Hard limit on unrealistic Z-velocity (prevents "flying away")
                if vz_mag > 5.0 {
                    vel[2] *= 5.0 / vz_mag;
                }
            }

            // === 7. Covariance Propagation (Position Block) ===
            // Simplified propagation: P_pos(t+dt) = P_pos(t) + Q_process * dt
            // This captures uncertainty growth due to model mismatch and process noise
            for j in 0..9 {
                p_pos[j] += q_pos_block[j] * dt;
            }

            // === 6. Record Trajectory Point ===
            trajectory.push(TrajectoryPoint {
                time: (i + 1) as f64 * dt,
                position: (pos[0], pos[1], pos[2]),
                velocity: (vel[0], vel[1], vel[2]),
                quaternion: (quat[0], quat[1], quat[2], quat[3]),
                covariance_pos: p_pos,
            });
        }

        trajectory
    }

    /// Check if GPS measurement is an outlier using prediction-based gating
    ///
    /// Uses Mahalanobis distance to reject GPS measurements that deviate
    /// significantly from the predicted trajectory.
    ///
    /// # Arguments
    /// * `gps_pos` - GPS position measurement (local ENU meters)
    /// * `accel_raw` - Latest IMU acceleration (for prediction)
    /// * `gyro_raw` - Latest IMU gyro (for prediction)
    /// * `dt_since_last` - Time since last filter update [seconds]
    /// * `sigma_threshold` - Rejection threshold in standard deviations (typically 3.0)
    ///
    /// # Returns
    /// (is_outlier, distance_meters)
    pub fn is_gps_outlier(
        &self,
        gps_pos: (f64, f64, f64),
        accel_raw: (f64, f64, f64),
        gyro_raw: (f64, f64, f64),
        dt_since_last: f64,
        _sigma_threshold: f64,
    ) -> (bool, f64) {
        // Predict where we should be at GPS timestamp
        // Use moderate decay (0.3) for outlier gating to account for deceleration patterns
        let num_steps = (dt_since_last / self.dt).ceil().max(1.0) as usize;
        let trajectory = self.predict_trajectory(accel_raw, gyro_raw, dt_since_last, num_steps, true, 0.3);

        let (predicted_pos, pred_cov_pos) = if let Some(last_point) = trajectory.last() {
            (last_point.position, last_point.covariance_pos)
        } else {
            // Fallback to current state if prediction fails
            let current_cov = [
                self.covariance[[0, 0]], self.covariance[[0, 1]], self.covariance[[0, 2]],
                self.covariance[[1, 0]], self.covariance[[1, 1]], self.covariance[[1, 2]],
                self.covariance[[2, 0]], self.covariance[[2, 1]], self.covariance[[2, 2]],
            ];
            ((self.state[0], self.state[1], self.state[2]), current_cov)
        };

        // Innovation vector: z - h*x (measurement - prediction)
        let innovation = [
            gps_pos.0 - predicted_pos.0,
            gps_pos.1 - predicted_pos.1,
            gps_pos.2 - predicted_pos.2,
        ];

        // GPS measurement noise covariance (diagonal)
        // Horizontal: 5m std dev, Vertical: 20m std dev
        let r_gps_noise = 5.0_f64.powi(2);
        let gps_cov = [
            r_gps_noise, 0.0, 0.0,
            0.0, r_gps_noise, 0.0,
            0.0, 0.0, (20.0_f64).powi(2),
        ];

        // Combined covariance: S = P_pred + R_gps (3x3)
        let mut s = [0.0; 9];
        for i in 0..9 {
            s[i] = pred_cov_pos[i] + gps_cov[i];
        }

        // Invert S (3x3) using nalgebra
        use nalgebra::Matrix3;
        let s_mat = Matrix3::new(
            s[0], s[1], s[2],
            s[3], s[4], s[5],
            s[6], s[7], s[8],
        );

        let mahal_distance = if let Some(s_inv_na) = s_mat.try_inverse() {
            // Convert inverse to array form
            let s_inv = [
                s_inv_na[(0, 0)], s_inv_na[(0, 1)], s_inv_na[(0, 2)],
                s_inv_na[(1, 0)], s_inv_na[(1, 1)], s_inv_na[(1, 2)],
                s_inv_na[(2, 0)], s_inv_na[(2, 1)], s_inv_na[(2, 2)],
            ];

            // Compute Mahalanobis distance: d = sqrt(innovation^T * S^-1 * innovation)
            // temp = S^-1 * innovation (3x1)
            let temp = [
                s_inv[0] * innovation[0] + s_inv[1] * innovation[1] + s_inv[2] * innovation[2],
                s_inv[3] * innovation[0] + s_inv[4] * innovation[1] + s_inv[5] * innovation[2],
                s_inv[6] * innovation[0] + s_inv[7] * innovation[1] + s_inv[8] * innovation[2],
            ];

            // d = sqrt(innovation^T * temp)
            let mahal_sq = innovation[0] * temp[0] + innovation[1] * temp[1] + innovation[2] * temp[2];
            mahal_sq.sqrt()
        } else {
            // Singular covariance: fallback to Euclidean distance
            (innovation[0].powi(2) + innovation[1].powi(2) + innovation[2].powi(2)).sqrt()
        };

        // Gating threshold: 3-sigma for Mahalanobis distance
        // For 3D measurement: chi-squared with 3 DOF, 3-sigma ≈ 3.0
        let sigma_threshold = 3.0;
        let is_outlier = mahal_distance > sigma_threshold;

        (is_outlier, mahal_distance)
    }

    /// GPS update with outlier rejection (recommended for live tracking)
    ///
    /// Wrapper around update_gps() that uses prediction-based gating.
    ///
    /// # Returns
    /// (accepted: bool, nis: f64) - Whether update was applied and NIS value
    pub fn update_gps_with_gating(
        &mut self,
        gps_pos: (f64, f64, f64),
        accuracy: f64,
        accel_raw: (f64, f64, f64),
        gyro_raw: (f64, f64, f64),
        dt_since_last: f64,
        timestamp: f64,
    ) -> (bool, f64) {
        let (is_outlier, distance) = self.is_gps_outlier(
            gps_pos,
            accel_raw,
            gyro_raw,
            dt_since_last,
            3.0, // 3-sigma threshold
        );

        if is_outlier {
            eprintln!(
                "GPS outlier rejected: {:.2} m deviation (prediction-based gating)",
                distance
            );
            (false, f64::INFINITY)
        } else {
            let nis = self.update_gps(gps_pos, accuracy, timestamp);
            (true, nis)
        }
    }

    /// GPS update: correct position with full Kalman update (FIXED)
    ///
    /// This replaces the broken scalar-only update with proper cross-covariance propagation.
    /// GPS position corrections now also adjust velocity estimates through P_pv.
    ///
    /// # Returns
    /// NIS (Normalized Innovation Squared) for filter consistency monitoring.
    /// Ideal average: ~3.0 for 3D position measurement.
    pub fn update_gps(&mut self, gps_pos: (f64, f64, f64), accuracy: f64, timestamp: f64) -> f64 {
        // Calculate speed for dynamic noise floor
        let vx = self.state[3];
        let vy = self.state[4];
        let vz = self.state[5];
        let speed = (vx * vx + vy * vy + vz * vz).sqrt();

        // Dynamic Floor: Trust Inertia at low speed, GPS at high speed
        // < 3 m/s: Floor = 10m (R=100)
        // > 15 m/s: Floor = 3m (R=9)
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

        // Innovation: z - H*x
        let innovation = arr1(&[
            pos_x - self.state[0],
            pos_y - self.state[1],
            pos_z - self.state[2],
        ]);

        // Divergence recovery: if we are very far from GPS but GPS accuracy is reasonable, snap to GPS
        let dist = (innovation[0].powi(2) + innovation[1].powi(2) + innovation[2].powi(2)).sqrt();
        // Snap threshold reduced to 30m to catch drifts when using high R at low speed
        if dist > 30.0 {
            if safe_accuracy < 20.0 {
                eprintln!(
                    "[EKF15D] Divergence detected (dist {:.1} m). Snapping state to GPS.",
                    dist
                );

                // Teleport position to GPS
                self.state[0] = pos_x;
                self.state[1] = pos_y;
                self.state[2] = pos_z;

                // Inferred velocity from drift over time since last accepted GPS (avoid rocket or zeroing)
                let effective_dt = self
                    .last_gps_time
                    .map(|t| (timestamp - t).max(self.dt))
                    .unwrap_or(self.dt); // default to one filter step if no history
                let drift_vec = if let Some((lx, ly, lz)) = self.last_gps_pos {
                    [
                        pos_x - lx,
                        pos_y - ly,
                        pos_z - lz,
                    ]
                } else {
                    [innovation[0], innovation[1], innovation[2]]
                };
                let inferred_v = if effective_dt > 1.0 && dist > 15.0 {
                    let mut raw_v = [
                        drift_vec[0] / effective_dt,
                        drift_vec[1] / effective_dt,
                        drift_vec[2] / effective_dt,
                    ];
                    // Clamp to plausible speeds (<= 35 m/s ~126 km/h)
                    let speed = (raw_v[0].powi(2) + raw_v[1].powi(2) + raw_v[2].powi(2)).sqrt();
                    if speed > 35.0 {
                        let scale = 35.0 / speed;
                        raw_v[0] *= scale;
                        raw_v[1] *= scale;
                        raw_v[2] *= scale;
                    }
                    raw_v
                } else {
                    // Small/short correction: keep velocity but damp it slightly
                    [
                        self.state[3] * 0.9,
                        self.state[4] * 0.9,
                        self.state[5] * 0.9,
                    ]
                };
                self.state[3] = inferred_v[0];
                self.state[4] = inferred_v[1];
                self.state[5] = inferred_v[2];

                // Reset covariance to soft trust on position and high uncertainty on velocity
                let mut p_reset = Array2::<f64>::eye(15);
                let pos_var = (safe_accuracy * 1.0).powi(2); // tighter trust on snap position
                p_reset[[0, 0]] = pos_var;
                p_reset[[1, 1]] = pos_var;
                p_reset[[2, 2]] = pos_var;
                p_reset[[3, 3]] = 400.0; // ~20 m/s std dev on vx
                p_reset[[4, 4]] = 400.0; // ~20 m/s std dev on vy
                p_reset[[5, 5]] = 100.0;  // ~10 m/s std dev on vz
                // Inflate bias variances slightly to allow re-convergence
                p_reset[[10, 10]] = p_reset[[10, 10]].max(0.01);
                p_reset[[11, 11]] = p_reset[[11, 11]].max(0.01);
                p_reset[[12, 12]] = p_reset[[12, 12]].max(0.01);
                p_reset[[13, 13]] = p_reset[[13, 13]].max(0.1);
                p_reset[[14, 14]] = p_reset[[14, 14]].max(0.1);
                self.covariance = p_reset;

                self.gps_updates += 1;
                self.last_gps_time = Some(timestamp);
                self.last_gps_pos = Some((pos_x, pos_y, pos_z));
                return 0.0;
            } else {
                // GPS is low-quality and far away: skip
                return f64::INFINITY;
            }
        }

        // Measurement matrix H (3x15): observes position states [0,1,2]
        let mut h = Array2::<f64>::zeros((3, 15));
        h[[0, 0]] = 1.0;
        h[[1, 1]] = 1.0;
        h[[2, 2]] = 1.0;

        // Measurement noise R (3x3)
        // Altitude typically has 2-4x worse accuracy than horizontal
        let mut r = Array2::<f64>::zeros((3, 3));
        r[[0, 0]] = gps_noise;
        r[[1, 1]] = gps_noise;
        r[[2, 2]] = gps_noise * 4.0; // vertical uncertainty higher

        // Innovation covariance: S = H*P*H^T + R
        let p = &self.covariance;
        let h_t = h.t();
        let s = h.dot(p).dot(&h_t) + r.clone();

        // Invert S (3x3) using nalgebra for numerical stability
        use nalgebra::Matrix3;
        let s_mat = Matrix3::new(
            s[[0, 0]], s[[0, 1]], s[[0, 2]],
            s[[1, 0]], s[[1, 1]], s[[1, 2]],
            s[[2, 0]], s[[2, 1]], s[[2, 2]],
        );

        let Some(s_inv_na) = s_mat.try_inverse() else {
            // Singular innovation covariance - skip update
            return f64::INFINITY; // Signal rejected update
        };

        // Convert back to ndarray
        let mut s_inv = Array2::<f64>::zeros((3, 3));
        for i in 0..3 {
            for j in 0..3 {
                s_inv[[i, j]] = s_inv_na[(i, j)];
            }
        }

        // Calculate NIS (Normalized Innovation Squared) for filter consistency check
        // NIS = innovation^T * S^-1 * innovation
        // For 3D position measurement, NIS should average ~3.0 if filter is well-tuned
        let nis = {
            let temp = s_inv.dot(&innovation);
            innovation.dot(&temp)
        };

        // Kalman gain: K = P*H^T*S^-1 (15x3)
        // This is the KEY difference - full 15x3 gain matrix
        let k = p.dot(&h_t).dot(&s_inv);

        // State update: x = x + K*innovation
        // ALL 15 states are updated, including velocity through cross-covariance!
        let dx = k.dot(&innovation);
        for i in 0..15 {
            self.state[i] += dx[i];
        }

        // Re-normalize quaternion after update
        let q_norm = (
            self.state[6].powi(2) + 
            self.state[7].powi(2) + 
            self.state[8].powi(2) + 
            self.state[9].powi(2)
        ).sqrt();
        if q_norm > 1e-6 {
            self.state[6] /= q_norm;
            self.state[7] /= q_norm;
            self.state[8] /= q_norm;
            self.state[9] /= q_norm;
        }

        // Joseph form covariance update: P = (I-KH)*P*(I-KH)^T + K*R*K^T
        // More numerically stable than standard form
        let i_mat = Array2::<f64>::eye(15);
        let kh = k.dot(&h);
        let i_minus_kh = &i_mat - &kh;
        let term1 = i_minus_kh.dot(p).dot(&i_minus_kh.t());
        let term2 = k.dot(&r).dot(&k.t());
        self.covariance = term1 + term2;

        // Symmetrize to prevent numerical drift
        let p_t = self.covariance.t().to_owned();
        self.covariance = (&self.covariance + &p_t) / 2.0;

        // Ensure positive definiteness (floor small variances)
        for i in 0..15 {
            if self.covariance[[i, i]] < 1e-9 {
                self.covariance[[i, i]] = 1e-9;
            }
        }

        self.gps_updates += 1;
        self.last_gps_time = Some(timestamp);
        self.last_gps_pos = Some((pos_x, pos_y, pos_z));

        // Return NIS for filter consistency monitoring
        nis
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

        // Extract roll/pitch/yaw from quaternion
        let q = nalgebra::UnitQuaternion::from_quaternion(nalgebra::Quaternion::new(
            self.state[6],
            self.state[7],
            self.state[8],
            self.state[9],
        ));
        let (roll, pitch, current_yaw) = q.euler_angles();

        // Tilt compensation
        let (sin_r, cos_r) = (roll.sin(), roll.cos());
        let (sin_p, cos_p) = (pitch.sin(), pitch.cos());
        let mag_x_h = mag.x * cos_p + mag.y * sin_r * sin_p + mag.z * cos_r * sin_p;
        let mag_y_h = mag.y * cos_r - mag.z * sin_r;
        let mag_yaw = mag_y_h.atan2(mag_x_h) + declination_rad; // ENU yaw (CCW from East)

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

        // Apply partial correction (poor-man's gain) preserving roll/pitch
        let gain = 0.3;
        let new_yaw = current_yaw + gain * innov;
        let new_q = nalgebra::UnitQuaternion::from_euler_angles(roll, pitch, new_yaw);
        let normalized_q = new_q.normalize();
        self.state[6] = normalized_q.w;
        self.state[7] = normalized_q.i;
        self.state[8] = normalized_q.j;
        self.state[9] = normalized_q.k;

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

    /// Non-holonomic constraint with mounting yaw offset compensation
    ///
    /// Constrains vehicle-frame lateral and vertical velocity to zero.
    /// `forward_speed`: expected forward speed in vehicle frame
    /// `mounting_yaw_offset`: rotation from phone body frame to vehicle frame (radians)
    /// `lateral_vertical_noise`: measurement noise for Y/Z constraints
    pub fn update_body_velocity_with_offset(
        &mut self,
        forward_speed: f64,
        mounting_yaw_offset: f64,
        lateral_vertical_noise: f64,
    ) {
        // === 1. Build phone body-to-world rotation from quaternion ===
        let mut qw = self.state[6];
        let mut qx = self.state[7];
        let mut qy = self.state[8];
        let mut qz = self.state[9];

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

        // R_body_to_world (phone frame)
        let r00 = 1.0 - 2.0 * (qy * qy + qz * qz);
        let r01 = 2.0 * (qx * qy - qw * qz);
        let r02 = 2.0 * (qx * qz + qw * qy);
        let r10 = 2.0 * (qx * qy + qw * qz);
        let r11 = 1.0 - 2.0 * (qx * qx + qz * qz);
        let r12 = 2.0 * (qy * qz - qw * qx);
        let r20 = 2.0 * (qx * qz - qw * qy);
        let r21 = 2.0 * (qy * qz + qw * qx);
        let r22 = 1.0 - 2.0 * (qx * qx + qy * qy);

        // R_phone_body_from_world = R^T
        let r_phone_t = Matrix3::new(
            r00, r10, r20,
            r01, r11, r21,
            r02, r12, r22,
        );

        // === 2. Build mounting offset rotation (yaw only, around Z) ===
        // R_vehicle_from_phone: rotates phone body frame to vehicle frame
        let cos_m = mounting_yaw_offset.cos();
        let sin_m = mounting_yaw_offset.sin();
        let r_mount = Matrix3::new(
            cos_m, -sin_m, 0.0,
            sin_m,  cos_m, 0.0,
            0.0,    0.0,   1.0,
        );

        // === 3. Combined H_vel: world velocity -> vehicle body velocity ===
        // v_vehicle = R_mount * R_phone^T * v_world
        let h_vel_na = r_mount * r_phone_t;

        // Convert to ndarray for compatibility
        let h_vel = Array2::from_shape_vec(
            (3, 3),
            vec![
                h_vel_na[(0, 0)], h_vel_na[(0, 1)], h_vel_na[(0, 2)],
                h_vel_na[(1, 0)], h_vel_na[(1, 1)], h_vel_na[(1, 2)],
                h_vel_na[(2, 0)], h_vel_na[(2, 1)], h_vel_na[(2, 2)],
            ],
        ).unwrap();

        // === 4. Predicted vehicle-frame velocity ===
        let v_world = arr1(&[self.state[3], self.state[4], self.state[5]]);
        let v_vehicle_pred = h_vel.dot(&v_world);

        // === 5. Measurement: [forward_speed, 0, 0] in vehicle frame ===
        let meas = arr1(&[forward_speed, 0.0, 0.0]);
        let innovation = &meas - &v_vehicle_pred;

        // === 6. Measurement noise (ignore forward, constrain lateral/vertical) ===
        let mut r = Matrix3::zeros();
        let r_yz = lateral_vertical_noise.max(1e-6);
        r[(0, 0)] = 999.0;  // Don't constrain forward speed
        r[(1, 1)] = r_yz;   // Constrain lateral (no sideslip)
        r[(2, 2)] = r_yz;   // Constrain vertical (ground vehicle)

        // === 7. Standard Kalman update with full cross-covariance ===

        // Extract velocity covariance block P_vv (3x3)
        let p_vv = self.covariance.slice(s![3..6, 3..6]).to_owned();
        let p_vv_mat = Matrix3::from_row_slice(p_vv.as_slice().unwrap());

        // S = H * P_vv * H^T + R
        let s_mat = h_vel_na * p_vv_mat * h_vel_na.transpose() + r;

        let Some(s_inv) = s_mat.try_inverse() else {
            return; // Singular, skip update
        };

        // Full cross-covariance: P[:, vel] (15 x 3)
        let p_vel = self.covariance.slice(s![.., 3..6]).to_owned();

        // K = P[:, vel] * H^T * S^-1
        let h_t = h_vel_na.transpose();
        let mut h_t_arr = Array2::<f64>::zeros((3, 3));
        let mut s_inv_arr = Array2::<f64>::zeros((3, 3));
        for i in 0..3 {
            for j in 0..3 {
                h_t_arr[[i, j]] = h_t[(i, j)];
                s_inv_arr[[i, j]] = s_inv[(i, j)];
            }
        }
        let k = p_vel.dot(&h_t_arr).dot(&s_inv_arr); // (15 x 3)

        // State update
        let dx = k.dot(&innovation);
        for i in 0..15 {
            self.state[i] += dx[i];
        }

        // === 8. Joseph form covariance update ===
        let mut h_full = Array2::<f64>::zeros((3, 15));
        for row in 0..3 {
            for col in 0..3 {
                h_full[[row, 3 + col]] = h_vel[[row, col]];
            }
        }

        let k_na = SMatrix::<f64, 15, 3>::from_row_slice(k.as_slice().unwrap());
        let h_na = SMatrix::<f64, 3, 15>::from_row_slice(h_full.as_slice().unwrap());
        let p_na = SMatrix::<f64, 15, 15>::from_row_slice(self.covariance.as_slice().unwrap());

        let identity = SMatrix::<f64, 15, 15>::identity();
        let i_minus_kh = identity - k_na.clone() * h_na.clone();
        let term1 = &i_minus_kh * p_na * i_minus_kh.transpose();
        let term2 = k_na.clone() * r * k_na.transpose();
        let joseph = term1 + term2;

        // Copy back and symmetrize
        for i in 0..15 {
            for j in 0..15 {
                self.covariance[[i, j]] = 0.5 * (joseph[(i, j)] + joseph[(j, i)]);
            }
            // Floor diagonal
            if self.covariance[[i, i]] < 1e-6 {
                self.covariance[[i, i]] = 1e-6;
            }
        }
    }

    /// Get the current speed (velocity magnitude) from the 15D state
    pub fn get_speed(&self) -> f64 {
        let vx = self.state[3];
        let vy = self.state[4];
        let vz = self.state[5];
        (vx * vx + vy * vy + vz * vz).sqrt()
    }

    /// Extract yaw (heading) from quaternion state
    /// Returns radians in ENU frame (0 = East, π/2 = North, counter-clockwise)
    pub fn get_heading(&self) -> f64 {
        let q = nalgebra::UnitQuaternion::from_quaternion(
            nalgebra::Quaternion::new(
                self.state[6],  // qw
                self.state[7],  // qx
                self.state[8],  // qy
                self.state[9],  // qz
            )
        );
        let (_, _, yaw) = q.euler_angles();
        yaw
    }

    /// Get current gyro bias estimates
    pub fn get_gyro_bias(&self) -> (f64, f64, f64) {
        (self.state[10], self.state[11], self.state[12])
    }

    /// Get covariance diagonal elements for diagnostics
    ///
    /// Returns position (m²) and velocity (m²/s²) uncertainty diagonals
    pub fn get_covariance_diagonals(&self) -> (f64, f64, f64, f64, f64, f64) {
        (
            self.covariance[[0, 0]], // p_pos_x
            self.covariance[[1, 1]], // p_pos_y
            self.covariance[[2, 2]], // p_pos_z
            self.covariance[[3, 3]], // p_vel_x
            self.covariance[[4, 4]], // p_vel_y
            self.covariance[[5, 5]], // p_vel_z
        )
    }

    /// Heading-aided gyro Z-axis bias update
    ///
    /// Uses GPS bearing rate vs integrated gyro to observe Z-axis bias.
    /// Only valid during straight, fast driving with good GPS.
    ///
    /// `gps_heading_rate`: rad/s, from GPS bearing differencing
    /// `gyro_z_raw`: rad/s, current Z-axis gyro reading
    /// `noise_std`: measurement noise (rad/s)
    pub fn update_gyro_bias_from_heading(
        &mut self,
        gps_heading_rate: f64,
        gyro_z_raw: f64,
        noise_std: f64,
    ) {
        // Predicted heading rate = gyro_z - bias_z
        let bias_z = self.state[12];
        let predicted_rate = gyro_z_raw - bias_z;

        // Innovation: GPS says this, gyro says that
        let innovation = gps_heading_rate - predicted_rate;

        // Measurement noise covariance (scalar)
        let r = noise_std * noise_std;

        // S = H * P * H^T + R
        // H = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, -1, 0, 0] (sparse)
        // H * P * H^T = P[12,12]
        let p_bias_z = self.covariance[[12, 12]];
        let s = p_bias_z + r;

        if s.abs() < 1e-12 {
            return; // Singular, skip update
        }

        // K = P * H^T / S
        // K[i] = P[i, 12] * (-1) / S = -P[i, 12] / S
        let mut k = Array1::<f64>::zeros(15);
        for i in 0..15 {
            k[i] = -self.covariance[[i, 12]] / s;
        }

        // State update: x = x + K * innovation
        for i in 0..15 {
            self.state[i] += k[i] * innovation;
        }

        // Covariance update: P = (I - K*H) * P
        // (I - K*H)[i,j] = I[i,j] - K[i]*H[j]
        // H[j] = -1 if j==12, else 0
        // So: (I - K*H)[i,j] = I[i,j] - K[i]*(-1) = I[i,j] + K[i]*1 if j==12
        //                       I[i,j] if j != 12
        let mut new_p = self.covariance.clone();

        // Update all elements
        for i in 0..15 {
            for j in 0..15 {
                if j == 12 {
                    // (I - K*H)[i,12] = I[i,12] + K[i]
                    new_p[[i, j]] = self.covariance[[i, j]] + k[i];
                } else {
                    // (I - K*H)[i,j] = I[i,j]
                    new_p[[i, j]] = self.covariance[[i, j]];
                }
            }
        }

        // Apply Kalman gain to all rows
        for i in 0..15 {
            for j in 0..15 {
                new_p[[i, j]] -= k[i] * self.covariance[[12, j]];
            }
        }

        // Symmetrize to maintain PSD
        for i in 0..15 {
            for j in i + 1..15 {
                let avg = 0.5 * (new_p[[i, j]] + new_p[[j, i]]);
                new_p[[i, j]] = avg;
                new_p[[j, i]] = avg;
            }
        }

        // Ensure diagonal stays positive
        for i in 0..15 {
            if new_p[[i, i]] < 1e-6 {
                new_p[[i, i]] = 1e-6;
            }
        }

        self.covariance = new_p;
    }

    /// Barometer altitude update using pressure measurement
    ///
    /// Converts barometric pressure to altitude and updates Z-axis position.
    /// Uses standard atmosphere model (valid up to ~11km).
    ///
    /// # Arguments
    /// * `baro`: Barometer data with pressure in hPa
    /// * `reference_pressure_hpa`: Sea-level or local reference pressure (default: 1013.25 hPa)
    /// * `noise_std`: Altitude measurement noise std dev [meters] (typical: 1-3m)
    ///
    /// # Returns
    /// NIS (Normalized Innovation Squared) for consistency monitoring
    pub fn update_barometer(
        &mut self,
        baro: &crate::types::BaroData,
        reference_pressure_hpa: f64,
        noise_std: f64,
    ) -> f64 {
        // Convert pressure to altitude using standard atmosphere model
        // h = 44330.0 * (1 - (P/P0)^0.1903)
        let altitude = 44330.0 * (1.0 - (baro.pressure_hpa / reference_pressure_hpa).powf(0.1903));

        // Innovation: measured altitude - current Z position
        let innovation = altitude - self.state[2];

        // Measurement matrix H (1x15): observes Z position (state[2])
        let mut h = Array1::<f64>::zeros(15);
        h[2] = 1.0;

        // Measurement noise R (scalar)
        let r = noise_std * noise_std;

        // Innovation covariance: S = H*P*H^T + R
        let p_zz = self.covariance[[2, 2]];
        let s = p_zz + r;

        if s.abs() < 1e-12 {
            return f64::INFINITY; // Singular, skip update
        }

        // Calculate NIS before update
        let nis = (innovation * innovation) / s;

        // Kalman gain: K = P*H^T / S
        // K[i] = P[i, 2] / S
        let mut k = Array1::<f64>::zeros(15);
        for i in 0..15 {
            k[i] = self.covariance[[i, 2]] / s;
        }

        // State update: x = x + K * innovation
        for i in 0..15 {
            self.state[i] += k[i] * innovation;
        }

        // Covariance update: P = (I - K*H) * P * (I - K*H)^T + K*R*K^T (Joseph form)
        // For scalar measurement: P_new[i,j] = P[i,j] - K[i]*P[2,j] - K[j]*P[i,2] + K[i]*K[j]*S
        let mut new_p = self.covariance.clone();
        for i in 0..15 {
            for j in 0..15 {
                new_p[[i, j]] = self.covariance[[i, j]]
                    - k[i] * self.covariance[[2, j]]
                    - k[j] * self.covariance[[i, 2]]
                    + k[i] * k[j] * s;
            }
        }

        // Symmetrize to maintain PSD
        for i in 0..15 {
            for j in i + 1..15 {
                let avg = 0.5 * (new_p[[i, j]] + new_p[[j, i]]);
                new_p[[i, j]] = avg;
                new_p[[j, i]] = avg;
            }
        }

        // Ensure diagonal stays positive
        for i in 0..15 {
            if new_p[[i, i]] < 1e-9 {
                new_p[[i, i]] = 1e-9;
            }
        }

        self.covariance = new_p;

        nis
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
