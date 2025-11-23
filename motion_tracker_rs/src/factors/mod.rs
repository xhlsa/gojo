/// Factor Graph Optimization (FGO) modules
///
/// Implements custom factors for visual-inertial odometry including
/// IMU preintegration, GPS factors, and graph management.

pub mod imu_preintegration;
pub mod gps;
pub mod graph_estimator;

pub use imu_preintegration::{PreintegratedImuMeasurements, ImuFactor};
pub use gps::GpsFactor;
pub use graph_estimator::{GraphEstimator, StateKeys, GraphFactor};
