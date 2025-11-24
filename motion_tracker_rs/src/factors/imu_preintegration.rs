/// IMU Preintegration Factor for Factor Graph Optimization (FGO)
///
/// Implements the standard Forster/VINS-Mono manifold theory for IMU preintegration.
/// This factor constrains two consecutive poses and velocities based on high-frequency
/// IMU measurements, accounting for sensor bias drift via first-order bias correction.
///
/// References:
/// - Forster et al., "On-Manifold Preintegration for Real-Time Visual-Inertial Odometry"
/// - Qin et al., "VINS-Mono: A Robust and Versatile Monocular Visual-Inertial State Estimator"
use nalgebra::{Matrix3, Matrix6, UnitQuaternion, Vector3, Vector6};

const G: f64 = 9.81; // Earth gravity (m/s²)

/// Preintegrated IMU Measurements between two keyframes
///
/// Stores the accumulated IMU data and Jacobians for bias correction.
/// All quantities are expressed in the body frame of the initial pose.
#[derive(Clone, Debug)]
pub struct PreintegratedImuMeasurements {
    /// Position delta: ∫∫ a(t) dt dt over time interval
    pub delta_p: Vector3<f64>,

    /// Velocity delta: ∫ a(t) dt over time interval
    pub delta_v: Vector3<f64>,

    /// Quaternion delta: exp(∫ ω(t) dt)
    pub delta_q: UnitQuaternion<f64>,

    /// Total time elapsed between keyframes [seconds]
    pub sum_dt: f64,

    /// Nominal accelerometer bias used during integration [m/s²]
    pub nominal_accel_bias: Vector3<f64>,

    /// Nominal gyroscope bias used during integration [rad/s]
    pub nominal_gyro_bias: Vector3<f64>,

    /// Jacobian of delta_p w.r.t. accelerometer bias: ∂Δp / ∂ba [3x3]
    pub dp_dba: Matrix3<f64>,

    /// Jacobian of delta_p w.r.t. gyroscope bias: ∂Δp / ∂bg [3x3]
    pub dp_dbg: Matrix3<f64>,

    /// Jacobian of delta_v w.r.t. accelerometer bias: ∂Δv / ∂ba [3x3]
    pub dv_dba: Matrix3<f64>,

    /// Jacobian of delta_v w.r.t. gyroscope bias: ∂Δv / ∂bg [3x3]
    pub dv_dbg: Matrix3<f64>,

    /// Jacobian of delta_q w.r.t. gyroscope bias: ∂Δq / ∂bg [3x3]
    pub dq_dbg: Matrix3<f64>,

    /// Covariance of measurement noise [6x6] for accel (0:3) and gyro (3:6)
    pub covariance: Matrix6<f64>,
}

impl PreintegratedImuMeasurements {
    /// Create a new preintegrated IMU measurement set
    pub fn new(accel_noise_std: f64, gyro_noise_std: f64) -> Self {
        let mut covariance = Matrix6::<f64>::zeros();

        // Accelerometer noise covariance [m²/s⁴]
        let accel_var = accel_noise_std * accel_noise_std;
        for i in 0..3 {
            covariance[(i, i)] = accel_var;
        }

        // Gyroscope noise covariance [rad²/s²]
        let gyro_var = gyro_noise_std * gyro_noise_std;
        for i in 3..6 {
            covariance[(i, i)] = gyro_var;
        }

        Self {
            delta_p: Vector3::zeros(),
            delta_v: Vector3::zeros(),
            delta_q: UnitQuaternion::identity(),
            sum_dt: 0.0,
            nominal_accel_bias: Vector3::zeros(),
            nominal_gyro_bias: Vector3::zeros(),
            dp_dba: Matrix3::zeros(),
            dp_dbg: Matrix3::zeros(),
            dv_dba: Matrix3::zeros(),
            dv_dbg: Matrix3::zeros(),
            dq_dbg: Matrix3::zeros(),
            covariance,
        }
    }

    /// Integrate a new accel and gyro measurement
    ///
    /// This performs the first-order integration with bias correction
    /// using the current nominal bias estimates.
    pub fn integrate_measurement(
        &mut self,
        accel: Vector3<f64>,
        gyro: Vector3<f64>,
        dt: f64,
        accel_bias: Vector3<f64>,
        gyro_bias: Vector3<f64>,
    ) {
        // Store nominal bias for this integration segment
        self.nominal_accel_bias = accel_bias;
        self.nominal_gyro_bias = gyro_bias;

        // Correct measurements for bias
        let accel_corrected = accel - accel_bias;
        let gyro_corrected = gyro - gyro_bias;

        // Integrate position: p += v*dt + 0.5*a*dt²
        self.delta_p += self.delta_v * dt + 0.5 * accel_corrected * dt * dt;

        // Integrate velocity: v += a*dt
        self.delta_v += accel_corrected * dt;

        // Integrate quaternion: q = q * exp(0.5*ω*dt)
        let gyro_mag = gyro_corrected.norm();
        if gyro_mag > 1e-8 {
            let half_angle = 0.5 * gyro_mag * dt;
            let axis = gyro_corrected / gyro_mag;
            let dq = UnitQuaternion::from_axis_angle(
                &nalgebra::Unit::new_normalize(axis),
                half_angle * 2.0,
            );
            self.delta_q = dq * self.delta_q;
        }

        self.sum_dt += dt;

        // Update Jacobians (simplified first-order approximation)
        // These would normally be updated iteratively during integration
        self.dp_dba += -0.5 * self.delta_q.to_rotation_matrix().matrix() * dt * dt;
        self.dv_dba += -self.delta_q.to_rotation_matrix().matrix() * dt;

        // Gyroscope bias Jacobian (affects rotation)
        let gyro_skew = skew_symmetric(&gyro_corrected);
        self.dq_dbg += -0.5 * gyro_skew * dt;
    }
}

/// IMU Preintegration Factor for Factor Graph Optimization
///
/// Constrains two consecutive poses and velocities based on integrated IMU measurements.
/// The residual is computed as the difference between predicted and actual state changes,
/// with first-order correction for bias drift.
#[derive(Clone, Debug)]
pub struct ImuFactor {
    /// Preintegrated measurements between keyframes
    pub preintegration: PreintegratedImuMeasurements,

    /// Information matrix (inverse of covariance) for weighted least-squares [6x6]
    pub information: Matrix6<f64>,
}

impl ImuFactor {
    /// Create a new IMU factor with preintegrated measurements
    pub fn new(preintegration: PreintegratedImuMeasurements) -> Self {
        // Information = Covariance^-1
        let information = preintegration
            .covariance
            .try_inverse()
            .unwrap_or_else(|| Matrix6::<f64>::identity());

        Self {
            preintegration,
            information,
        }
    }

    /// Compute the residual vector for this factor
    ///
    /// # Arguments
    /// * `pose_i` - Rotation matrix (3x3) from world to body frame at time i
    /// * `pos_i` - Position of body at time i (world frame)
    /// * `vel_i` - Velocity of body at time i (world frame)
    /// * `bias_i` - Current bias estimate [accel (0:3), gyro (3:6)]
    /// * `pose_j` - Rotation matrix from world to body frame at time j
    /// * `pos_j` - Position of body at time j (world frame)
    /// * `vel_j` - Velocity of body at time j (world frame)
    ///
    /// # Returns
    /// Residual vector [position error (0:3), velocity error (3:5), rotation error (6:8)]
    pub fn compute_residual(
        &self,
        pose_i: &Matrix3<f64>,
        pos_i: &Vector3<f64>,
        vel_i: &Vector3<f64>,
        bias_i: &Vector6<f64>,
        pose_j: &Matrix3<f64>,
        pos_j: &Vector3<f64>,
        vel_j: &Vector3<f64>,
    ) -> Vector6<f64> {
        // Extract bias components
        let accel_bias = bias_i.fixed_rows::<3>(0).into_owned();
        let gyro_bias = bias_i.fixed_rows::<3>(3).into_owned();

        // Compute bias error (difference from nominal)
        let delta_ba = accel_bias - self.preintegration.nominal_accel_bias;
        let delta_bg = gyro_bias - self.preintegration.nominal_gyro_bias;

        // First-order bias correction
        let corrected_dp = self.preintegration.delta_p
            + self.preintegration.dp_dba * delta_ba
            + self.preintegration.dp_dbg * delta_bg;

        let corrected_dv = self.preintegration.delta_v
            + self.preintegration.dv_dba * delta_ba
            + self.preintegration.dv_dbg * delta_bg;

        // Quaternion correction: dq' = dq * exp(dr_dbg * dbg)
        let dr_dbg = self.preintegration.dq_dbg * delta_bg;
        let dq_correction = if dr_dbg.norm() > 1e-8 {
            let axis = dr_dbg.normalize();
            UnitQuaternion::from_axis_angle(&nalgebra::Unit::new_normalize(axis), dr_dbg.norm())
        } else {
            UnitQuaternion::identity()
        };
        let corrected_dq = dq_correction * self.preintegration.delta_q;

        // Compute position error in body frame of i
        // Expected position change: Ri^T * (Pj - Pi - Vi*Dt - 0.5*g*Dt²)
        let gravity_offset = Vector3::new(
            0.0,
            0.0,
            -0.5 * G * self.preintegration.sum_dt * self.preintegration.sum_dt,
        );
        let pos_change = pos_j - pos_i - vel_i * self.preintegration.sum_dt - gravity_offset;
        let pos_error = pose_i.transpose() * pos_change - corrected_dp;

        // Compute velocity error in body frame of i
        // Expected velocity change: Ri^T * (Vj - Vi - g*Dt)
        let vel_change = vel_j - vel_i - Vector3::new(0.0, 0.0, -G * self.preintegration.sum_dt);
        let vel_error = pose_i.transpose() * vel_change - corrected_dv;

        // Compute rotation error
        // R_error = Log(corrected_dq^-1 * Ri^T * Rj)
        let r_i_to_j = pose_i.transpose() * pose_j;
        let dq_rotation = UnitQuaternion::from_matrix(&r_i_to_j);
        let rot_error_q = corrected_dq.inverse() * dq_rotation;
        let rot_error = log_quaternion(&rot_error_q);

        // Stack residuals [position (0:3), velocity (3:5), rotation (6:8)]
        let mut residual = Vector6::zeros();
        residual.fixed_rows_mut::<3>(0).copy_from(&pos_error);
        residual.fixed_rows_mut::<3>(3).copy_from(&vel_error);

        // For rotation, we use the vector part of the exponential map
        if rot_error.norm() > 1e-8 {
            let rot_axis = rot_error.normalize();
            residual[6] = rot_error.norm() * rot_axis[0];
            residual[7] = rot_error.norm() * rot_axis[1];
            residual[8] = rot_error.norm() * rot_axis[2];
        }

        residual
    }

    /// Compute the weighted residual (residual^T * Information * residual)
    pub fn compute_weighted_error(
        &self,
        pose_i: &Matrix3<f64>,
        pos_i: &Vector3<f64>,
        vel_i: &Vector3<f64>,
        bias_i: &Vector6<f64>,
        pose_j: &Matrix3<f64>,
        pos_j: &Vector3<f64>,
        vel_j: &Vector3<f64>,
    ) -> f64 {
        let residual = self.compute_residual(pose_i, pos_i, vel_i, bias_i, pose_j, pos_j, vel_j);

        (residual.transpose() * self.information * residual)[0]
    }
}

/// Create a skew-symmetric matrix from a 3D vector
fn skew_symmetric(v: &Vector3<f64>) -> Matrix3<f64> {
    Matrix3::new(0.0, -v[2], v[1], v[2], 0.0, -v[0], -v[1], v[0], 0.0)
}

/// Compute the logarithm of a unit quaternion (returns 3D rotation vector)
///
/// For a quaternion q = [w, x, y, z], log(q) = (x, y, z) / sin(θ) * θ
/// where θ = acos(w)
fn log_quaternion(q: &UnitQuaternion<f64>) -> Vector3<f64> {
    let q_vec = q.as_ref();
    let w = q_vec[0];
    let xyz = Vector3::new(q_vec[1], q_vec[2], q_vec[3]);

    let norm_xyz = xyz.norm();
    if norm_xyz < 1e-8 {
        // Near identity: log(q) ≈ xyz
        xyz
    } else {
        let theta = w.atan2(norm_xyz);
        let scale = theta / norm_xyz;
        scale * xyz
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_preintegration_creation() {
        let preint = PreintegratedImuMeasurements::new(0.1, 0.001);
        assert_eq!(preint.sum_dt, 0.0);
        assert!(preint.delta_p.norm() < 1e-10);
        assert!(preint.delta_v.norm() < 1e-10);
    }

    #[test]
    fn test_imu_factor_creation() {
        let preint = PreintegratedImuMeasurements::new(0.1, 0.001);
        let factor = ImuFactor::new(preint);
        assert!(factor.information.determinant() > 0.0);
    }

    #[test]
    fn test_residual_zero_when_consistent() {
        let preint = PreintegratedImuMeasurements::new(0.1, 0.001);
        let factor = ImuFactor::new(preint);

        // Identity poses
        let pose_i = Matrix3::identity();
        let pose_j = Matrix3::identity();
        let pos_i = Vector3::zeros();
        let pos_j = Vector3::zeros();
        let vel_i = Vector3::zeros();
        let vel_j = Vector3::zeros();
        let bias_i = Vector6::zeros();

        let residual =
            factor.compute_residual(&pose_i, &pos_i, &vel_i, &bias_i, &pose_j, &pos_j, &vel_j);

        // Residual should be small when states are consistent with zero motion
        assert!(residual.norm() < 0.1);
    }
}
