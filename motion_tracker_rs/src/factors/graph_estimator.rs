use crate::factors::{GpsFactor, ImuFactor, PreintegratedImuMeasurements};
/// Factor Graph Optimizer (FGO) Manager for Visual-Inertial Odometry
///
/// Manages the factor graph lifecycle, keyframe creation, IMU preintegration,
/// and optimization loop. Coordinates between IMU, GPS, and bias factors.
use nalgebra::{Matrix3, Vector3, Vector6};

/// Key indices for pose, velocity, and bias variables in the factor graph
#[derive(Clone, Copy, Debug)]
pub struct StateKeys {
    /// Index of the current pose variable (SE3)
    pub pose_key: usize,

    /// Index of the current velocity variable (Vector3)
    pub vel_key: usize,

    /// Index of the current bias variable (Vector6: [accel_x, accel_y, accel_z, gyro_x, gyro_y, gyro_z])
    pub bias_key: usize,
}

/// Graph-based state estimator using Factor Graph Optimization
///
/// This manages the lifecycle of the factor graph for visual-inertial odometry:
/// 1. Accumulate IMU measurements between keyframes
/// 2. On GPS trigger, create keyframe and add factors
/// 3. Optimize the graph
/// 4. Reset preintegration for next keyframe cycle
#[derive(Clone, Debug)]
pub struct GraphEstimator {
    /// Current state variable keys (pose, velocity, bias)
    pub current_keys: StateKeys,

    /// Active preintegration accumulating measurements since last keyframe
    pub preintegration: PreintegratedImuMeasurements,

    /// Timestamp of last keyframe (seconds)
    pub last_stamp: f64,

    /// List of accumulated factors (simplified - in production use gtsam/g2o)
    pub factors: Vec<GraphFactor>,

    /// Origin latitude/longitude for local ENU conversion
    pub origin: Option<(f64, f64)>,

    /// Next available key for new variables
    next_key: usize,

    /// GPS noise standard deviation (meters)
    gps_noise_std: f64,

    /// Accel noise standard deviation (m/s²)
    accel_noise_std: f64,

    /// Gyro noise standard deviation (rad/s)
    gyro_noise_std: f64,

    /// Accelerometer bias random walk noise (m/s³)
    q_accel_bias: f64,

    /// Gyroscope bias random walk noise (rad/s²)
    q_gyro_bias: f64,
}

/// Enumeration of factors that can be added to the graph
#[derive(Clone, Debug)]
pub enum GraphFactor {
    /// IMU preintegration connecting two poses/velocities
    Imu {
        pose_i: usize,
        vel_i: usize,
        bias_i: usize,
        pose_j: usize,
        vel_j: usize,
        factor: ImuFactor,
    },

    /// GPS position measurement constraint
    Gps { pose_key: usize, factor: GpsFactor },

    /// Bias random walk (zero-mean drift between keyframes)
    BiasRandomWalk {
        bias_i: usize,
        bias_j: usize,
        noise_accel: f64,
        noise_gyro: f64,
    },

    /// Prior constraint for first keyframe
    Prior {
        var_key: usize,
        measurement: Vector3<f64>,
        noise: f64,
    },
}

impl GraphEstimator {
    /// Create a new graph estimator
    pub fn new(gps_noise_std: f64, accel_noise_std: f64, gyro_noise_std: f64) -> Self {
        let preintegration = PreintegratedImuMeasurements::new(accel_noise_std, gyro_noise_std);

        // Initialize with zero keys (will be assigned on first keyframe)
        let current_keys = StateKeys {
            pose_key: 0,
            vel_key: 1,
            bias_key: 2,
        };

        Self {
            current_keys,
            preintegration,
            last_stamp: 0.0,
            factors: Vec::new(),
            origin: None,
            next_key: 3,
            gps_noise_std,
            accel_noise_std,
            gyro_noise_std,
            q_accel_bias: 0.001 * accel_noise_std * accel_noise_std,
            q_gyro_bias: 0.01 * gyro_noise_std * gyro_noise_std,
        }
    }

    /// Enqueue an IMU measurement for accumulation
    ///
    /// # Arguments
    /// * `accel` - Raw accelerometer reading [m/s²]
    /// * `gyro` - Raw gyroscope reading [rad/s]
    /// * `dt` - Time step since last measurement [seconds]
    /// * `accel_bias` - Current accel bias estimate [m/s²]
    /// * `gyro_bias` - Current gyro bias estimate [rad/s]
    pub fn enqueue_imu(
        &mut self,
        accel: Vector3<f64>,
        gyro: Vector3<f64>,
        dt: f64,
        accel_bias: Vector3<f64>,
        gyro_bias: Vector3<f64>,
    ) {
        // Simply accumulate the measurement
        // The actual integration happens in PreintegratedImuMeasurements::integrate_measurement
        self.preintegration
            .integrate_measurement(accel, gyro, dt, accel_bias, gyro_bias);
    }

    /// Add a GPS measurement, triggering keyframe creation and graph optimization
    ///
    /// # Arguments
    /// * `lat` - Latitude [degrees]
    /// * `lon` - Longitude [degrees]
    /// * `alt` - Altitude [meters, unused - can use 0.0]
    /// * `timestamp` - Current timestamp [seconds]
    /// * `current_bias` - Current bias estimate [accel_x, accel_y, accel_z, gyro_x, gyro_y, gyro_z]
    ///
    /// # Returns
    /// Updated state keys for the new keyframe
    pub fn add_gps_measurement(
        &mut self,
        lat: f64,
        lon: f64,
        _alt: f64,
        timestamp: f64,
        gps_speed: f64,
        current_bias: Vector6<f64>,
    ) -> StateKeys {
        // Initialize origin on first GPS fix
        if self.origin.is_none() {
            self.origin = Some((lat, lon));
        }

        let (origin_lat, origin_lon) = self.origin.unwrap();

        // Convert lat/lon to local ENU coordinates
        let gps_pos_enu = latlon_to_enu(lat, lon, origin_lat, origin_lon);

        // Create new state variables for keyframe k+1
        let pose_k1 = self.next_key;
        let vel_k1 = self.next_key + 1;
        let bias_k1 = self.next_key + 2;
        self.next_key += 3;

        // Add IMU preintegration factor between k and k+1
        let imu_factor = ImuFactor::new(self.preintegration.clone());
        self.factors.push(GraphFactor::Imu {
            pose_i: self.current_keys.pose_key,
            vel_i: self.current_keys.vel_key,
            bias_i: self.current_keys.bias_key,
            pose_j: pose_k1,
            vel_j: vel_k1,
            factor: imu_factor,
        });

        // Add GPS factor constraint on position at keyframe k+1
        let gps_factor = GpsFactor::new(pose_k1, gps_pos_enu, self.gps_noise_std);
        self.factors.push(GraphFactor::Gps {
            pose_key: pose_k1,
            factor: gps_factor,
        });

        // If GPS reports stationary, enforce zero-velocity prior to prevent drift
        if gps_speed < 0.2 {
            self.factors.push(GraphFactor::Prior {
                var_key: vel_k1,
                measurement: Vector3::zeros(),
                noise: 1e-9, // effectively clamps velocity to zero
            });
        }

        // Add bias random walk factor (zero-mean drift from k to k+1)
        self.factors.push(GraphFactor::BiasRandomWalk {
            bias_i: self.current_keys.bias_key,
            bias_j: bias_k1,
            noise_accel: self.q_accel_bias,
            noise_gyro: self.q_gyro_bias,
        });

        // Optimization would happen here with a real factor graph library
        // For now, we track factors and state for serialization
        self.optimize();

        // Reset preintegration with new bias for next cycle
        self.last_stamp = timestamp;
        self.preintegration =
            PreintegratedImuMeasurements::new(self.accel_noise_std, self.gyro_noise_std);
        // Set nominal bias to current estimate for next integration cycle
        self.preintegration.nominal_accel_bias = current_bias.fixed_rows::<3>(0).into_owned();
        self.preintegration.nominal_gyro_bias = current_bias.fixed_rows::<3>(3).into_owned();

        // Update current state to point to new keyframe
        let new_keys = StateKeys {
            pose_key: pose_k1,
            vel_key: vel_k1,
            bias_key: bias_k1,
        };
        self.current_keys = new_keys;

        new_keys
    }

    /// Optimize the factor graph
    ///
    /// In a production system, this would call GTSAM/g2o optimization.
    /// Here we perform a placeholder optimization (no-op for now).
    fn optimize(&mut self) {
        // TODO: Implement actual graph optimization with factor graph library
        // For now, factors are accumulated for analysis/serialization
        // In production, would call:
        //   let result = optimizer.optimize(self.factors);
        //   self.extract_and_update_state(result);
    }

    /// Reset the graph for a new estimation window
    pub fn reset(&mut self) {
        self.factors.clear();
        self.preintegration =
            PreintegratedImuMeasurements::new(self.accel_noise_std, self.gyro_noise_std);
        self.current_keys = StateKeys {
            pose_key: 0,
            vel_key: 1,
            bias_key: 2,
        };
        self.next_key = 3;
        self.last_stamp = 0.0;
    }

    /// Get the current number of factors in the graph
    pub fn num_factors(&self) -> usize {
        self.factors.len()
    }

    /// Get the current number of state variables in the graph
    pub fn num_variables(&self) -> usize {
        self.next_key
    }

    /// Get the last state keys
    pub fn get_current_keys(&self) -> StateKeys {
        self.current_keys
    }

    /// Get preintegration data (for analysis)
    pub fn get_preintegration(&self) -> &PreintegratedImuMeasurements {
        &self.preintegration
    }
}

/// Convert latitude/longitude to local ENU (East-North-Up) coordinates
///
/// # Arguments
/// * `lat` - Latitude [degrees]
/// * `lon` - Longitude [degrees]
/// * `origin_lat` - Reference latitude [degrees]
/// * `origin_lon` - Reference longitude [degrees]
///
/// # Returns
/// ENU position as [East, North, Up] in meters
fn latlon_to_enu(lat: f64, lon: f64, origin_lat: f64, origin_lon: f64) -> Vector3<f64> {
    const R: f64 = 6_371_000.0; // Earth radius in meters

    let d_lat = (lat - origin_lat).to_radians();
    let d_lon = (lon - origin_lon).to_radians();

    // East: R * dlon * cos(lat)
    let east = R * d_lon * origin_lat.to_radians().cos();

    // North: R * dlat
    let north = R * d_lat;

    // Up: 0 (ignoring altitude difference)
    let up = 0.0;

    Vector3::new(east, north, up)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_graph_estimator_creation() {
        let estimator = GraphEstimator::new(8.0, 0.5, 0.1);
        assert_eq!(estimator.num_factors(), 0);
        assert_eq!(estimator.num_variables(), 3); // Initial: pose, vel, bias
    }

    #[test]
    fn test_enqueue_imu() {
        let mut estimator = GraphEstimator::new(8.0, 0.5, 0.1);
        let accel = Vector3::new(0.0, 0.0, 9.81);
        let gyro = Vector3::zeros();
        let dt = 0.01;
        let bias_a = Vector3::zeros();
        let bias_g = Vector3::zeros();

        estimator.enqueue_imu(accel, gyro, dt, bias_a, bias_g);

        // Preintegration should accumulate
        assert!(estimator.preintegration.sum_dt > 0.0);
    }

    #[test]
    fn test_gps_keyframe_creation() {
        let mut estimator = GraphEstimator::new(8.0, 0.5, 0.1);

        // Queue some IMU data
        let accel = Vector3::new(0.0, 0.0, 9.81);
        let gyro = Vector3::zeros();
        for _ in 0..10 {
            estimator.enqueue_imu(accel, gyro, 0.01, Vector3::zeros(), Vector3::zeros());
        }

        // Add GPS fix (triggers keyframe)
        let bias = Vector6::zeros();
        let new_keys = estimator.add_gps_measurement(40.0, -74.0, 0.0, 1.0, 0.0, bias);

        // Graph should have factors
        assert!(estimator.num_factors() >= 2); // At least IMU + GPS factors
        assert_eq!(new_keys.pose_key, 3); // New pose key
        assert_eq!(new_keys.vel_key, 4); // New vel key
        assert_eq!(new_keys.bias_key, 5); // New bias key
    }

    #[test]
    fn test_enu_conversion() {
        // Test at reference point
        let pos = latlon_to_enu(40.0, -74.0, 40.0, -74.0);
        assert!(pos[0].abs() < 0.1); // East should be ~0
        assert!(pos[1].abs() < 0.1); // North should be ~0
    }

    #[test]
    fn test_enu_offset() {
        // Test offset (1 degree lat ~ 111 km)
        let pos = latlon_to_enu(40.01, -74.0, 40.0, -74.0);
        assert!(pos[1] > 1000.0); // North should be ~1000+ meters
    }
}
