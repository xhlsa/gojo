//! Linear algebra type system for motion tracker
//!
//! Provides compile-time dimension checking and clean type aliases
//! for all Kalman filter implementations.

use nalgebra::{SMatrix, SVector};

// ===== State Dimensions =====
pub const STATE_DIM_15: usize = 15;
pub const STATE_DIM_13: usize = 13;

// ===== Measurement Dimensions =====
pub const MEASURE_DIM_GPS_POS: usize = 3;  // (x, y, z)
pub const MEASURE_DIM_GPS_VEL: usize = 3;  // (vx, vy, vz)
pub const MEASURE_DIM_MAG: usize = 1;      // heading
pub const MEASURE_DIM_BARO: usize = 1;     // altitude

// ===== 15-State Filter Types =====
pub type StateVec15 = SVector<f64, STATE_DIM_15>;
pub type StateMat15 = SMatrix<f64, STATE_DIM_15, STATE_DIM_15>;

// Measurement types for 15-state filter
pub type GpsPosVec = SVector<f64, MEASURE_DIM_GPS_POS>;
pub type GpsPosNoise = SMatrix<f64, MEASURE_DIM_GPS_POS, MEASURE_DIM_GPS_POS>;
pub type GpsVelVec = SVector<f64, MEASURE_DIM_GPS_VEL>;
pub type GpsVelNoise = SMatrix<f64, MEASURE_DIM_GPS_VEL, MEASURE_DIM_GPS_VEL>;

// Kalman gain types
pub type KalmanGainGpsPos = SMatrix<f64, STATE_DIM_15, MEASURE_DIM_GPS_POS>;  // 15×3
pub type KalmanGainGpsVel = SMatrix<f64, STATE_DIM_15, MEASURE_DIM_GPS_VEL>;  // 15×3

// Jacobian types
pub type JacobianGpsPos = SMatrix<f64, MEASURE_DIM_GPS_POS, STATE_DIM_15>;  // 3×15

// ===== 13-State Filter Types =====
pub type StateVec13 = SVector<f64, STATE_DIM_13>;
pub type StateMat13 = SMatrix<f64, STATE_DIM_13, STATE_DIM_13>;

// ===== Sigma Point Types (for UKF) =====
pub const SIGMA_COUNT_15: usize = 2 * STATE_DIM_15 + 1;  // 31
pub type SigmaPoints15 = [StateVec15; SIGMA_COUNT_15];
pub type SigmaWeights = SVector<f64, SIGMA_COUNT_15>;
