/// GPS Unary Factor for Factor Graph Optimization
///
/// Constrains a pose variable to a GPS measurement in local ENU coordinates.
/// Simple position pinning factor that minimizes the difference between
/// estimated pose position and GPS measurement.
use nalgebra::{Matrix3, Vector3};

/// GPS Measurement Factor - Unary factor constraining position to GPS measurement
///
/// Attaches to a single pose variable and provides a measurement constraint
/// based on GPS (or other absolute position measurement).
#[derive(Clone, Debug)]
pub struct GpsFactor {
    /// Key/ID of the pose variable this factor constrains
    pub pose_key: usize,

    /// GPS measurement in local ENU coordinates [East, North, Up] (meters)
    pub measurement: Vector3<f64>,

    /// Information matrix (inverse of covariance) [3x3]
    /// Diagonal entries represent confidence in each direction
    pub information: Matrix3<f64>,
}

impl GpsFactor {
    /// Create a new GPS factor
    ///
    /// # Arguments
    /// * `pose_key` - Key of the pose variable to constrain
    /// * `measurement` - GPS position in local ENU [East, North, Up] (meters)
    /// * `gps_noise_std` - Standard deviation of GPS noise (meters)
    pub fn new(pose_key: usize, measurement: Vector3<f64>, gps_noise_std: f64) -> Self {
        let gps_var = gps_noise_std * gps_noise_std;

        // Create information matrix (inverse of covariance)
        let mut information = Matrix3::<f64>::zeros();
        for i in 0..3 {
            information[(i, i)] = 1.0 / gps_var;
        }

        Self {
            pose_key,
            measurement,
            information,
        }
    }

    /// Compute the residual for this factor
    ///
    /// # Arguments
    /// * `pose_position` - Estimated position from the graph [East, North, Up]
    ///
    /// # Returns
    /// Position error vector [3D] in local frame
    pub fn compute_residual(&self, pose_position: &Vector3<f64>) -> Vector3<f64> {
        pose_position - self.measurement
    }

    /// Compute the weighted squared error (Mahalanobis distance)
    ///
    /// # Arguments
    /// * `pose_position` - Estimated position from the graph
    ///
    /// # Returns
    /// Scalar error: residual^T * Information * residual
    pub fn compute_weighted_error(&self, pose_position: &Vector3<f64>) -> f64 {
        let residual = self.compute_residual(pose_position);
        (residual.transpose() * self.information * residual)[0]
    }

    /// Get the information matrix (inverse covariance)
    pub fn get_information(&self) -> &Matrix3<f64> {
        &self.information
    }

    /// Get the pose key
    pub fn get_pose_key(&self) -> usize {
        self.pose_key
    }

    /// Get the measurement
    pub fn get_measurement(&self) -> &Vector3<f64> {
        &self.measurement
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_gps_factor_creation() {
        let measurement = Vector3::new(100.0, 200.0, 50.0);
        let factor = GpsFactor::new(0, measurement, 5.0);

        assert_eq!(factor.pose_key, 0);
        assert_eq!(factor.measurement, measurement);
    }

    #[test]
    fn test_gps_residual_zero() {
        let measurement = Vector3::new(100.0, 200.0, 50.0);
        let factor = GpsFactor::new(0, measurement, 5.0);

        let residual = factor.compute_residual(&measurement);
        assert!(residual.norm() < 1e-10);
    }

    #[test]
    fn test_gps_residual_nonzero() {
        let measurement = Vector3::new(100.0, 200.0, 50.0);
        let factor = GpsFactor::new(0, measurement, 5.0);

        let estimated = Vector3::new(105.0, 195.0, 50.0);
        let residual = factor.compute_residual(&estimated);

        assert!((residual - Vector3::new(5.0, -5.0, 0.0)).norm() < 1e-10);
    }

    #[test]
    fn test_gps_weighted_error() {
        let measurement = Vector3::new(100.0, 200.0, 50.0);
        let factor = GpsFactor::new(0, measurement, 1.0);

        let estimated = Vector3::new(101.0, 201.0, 51.0);
        let error = factor.compute_weighted_error(&estimated);

        // Error should be: [1, 1, 1]^T * I * [1, 1, 1] where I = 1.0
        // = 1 + 1 + 1 = 3.0
        assert!((error - 3.0).abs() < 1e-10);
    }
}
