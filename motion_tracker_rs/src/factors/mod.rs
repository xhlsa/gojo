/// Factor Graph Optimization (FGO) modules
///
/// Implements custom factors for visual-inertial odometry including
/// IMU preintegration, GPS factors, and prior factors.

pub mod imu_preintegration;

pub use imu_preintegration::{PreintegratedImuMeasurements, ImuFactor};
