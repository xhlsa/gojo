#![allow(dead_code)]

//! Factor Graph Optimization (FGO) for trajectory estimation
//!
//! This module implements a factor graph-based SLAM approach with:
//! - IMU preintegration (fast 50Hz loop)
//! - GPS factors (slow 1Hz loop)
//! - Incremental optimization using iSAM2-style approach
//!
//! Status: Shadow Mode - Logs FGO estimates alongside EKF for comparison

use nalgebra::{Matrix3, Matrix6, Vector3, Vector6};
use serde::{Deserialize, Serialize};
use std::collections::VecDeque;

const GRAVITY: f64 = 9.81;

/// FGO state estimate (position, velocity, biases)
#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct FgoState {
    pub position: [f64; 3],     // [x, y, z] in meters
    pub velocity: [f64; 3],     // [vx, vy, vz] in m/s
    pub accel_bias: [f64; 3],   // Accelerometer bias
    pub gyro_bias: [f64; 3],    // Gyroscope bias
    pub timestamp: f64,
}

/// Preintegrated IMU measurement between two keyframes
struct PreintegratedImu {
    delta_position: Vector3<f64>,
    delta_velocity: Vector3<f64>,
    delta_rotation: Matrix3<f64>,
    dt_sum: f64,
    num_samples: usize,
}

impl PreintegratedImu {
    fn new() -> Self {
        Self {
            delta_position: Vector3::zeros(),
            delta_velocity: Vector3::zeros(),
            delta_rotation: Matrix3::identity(),
            dt_sum: 0.0,
            num_samples: 0,
        }
    }

    fn integrate(&mut self, accel: Vector3<f64>, gyro: Vector3<f64>, dt: f64) {
        // Simple Euler integration (real implementation would use Runge-Kutta)
        let gyro_norm = gyro.norm();

        // Rotate acceleration to world frame
        let accel_world = self.delta_rotation * accel;

        // Update velocity and position
        self.delta_velocity += accel_world * dt;
        self.delta_position += self.delta_velocity * dt + 0.5 * accel_world * dt * dt;

        // Update rotation (simplified - real impl needs proper SO(3) integration)
        if gyro_norm > 1e-6 {
            let angle = gyro_norm * dt;
            let axis = gyro / gyro_norm;
            let skew = Matrix3::new(
                0.0, -axis[2], axis[1],
                axis[2], 0.0, -axis[0],
                -axis[1], axis[0], 0.0,
            );
            let rot_delta = Matrix3::identity() + skew * angle.sin()
                + skew * skew * (1.0 - angle.cos());
            self.delta_rotation = self.delta_rotation * rot_delta;
        }

        self.dt_sum += dt;
        self.num_samples += 1;
    }

    fn reset(&mut self) {
        self.delta_position = Vector3::zeros();
        self.delta_velocity = Vector3::zeros();
        self.delta_rotation = Matrix3::identity();
        self.dt_sum = 0.0;
        self.num_samples = 0;
    }
}

/// GPS measurement factor
struct GpsFactor {
    position: Vector3<f64>,
    timestamp: f64,
    covariance: Matrix3<f64>,
}

/// Graph node (keyframe state)
struct GraphNode {
    position: Vector3<f64>,
    velocity: Vector3<f64>,
    accel_bias: Vector3<f64>,
    gyro_bias: Vector3<f64>,
    timestamp: f64,
}

/// Factor Graph Optimizer
pub struct GraphEstimator {
    // Current state
    current_position: Vector3<f64>,
    current_velocity: Vector3<f64>,
    current_accel_bias: Vector3<f64>,
    current_gyro_bias: Vector3<f64>,
    current_timestamp: f64,

    // Preintegration buffer (fast loop)
    preintegrator: PreintegratedImu,
    imu_queue: VecDeque<(Vector3<f64>, Vector3<f64>, f64)>,  // (accel, gyro, timestamp)

    // Graph structure
    nodes: VecDeque<GraphNode>,
    gps_factors: Vec<GpsFactor>,

    // Configuration
    max_nodes: usize,
    gps_noise_std: f64,
    imu_noise_std: f64,

    // State
    last_optimization_time: f64,
    optimization_count: usize,
}

impl GraphEstimator {
    /// Create new FGO estimator
    pub fn new(start_pos: (f64, f64, f64), start_vel: (f64, f64, f64), start_bias: (f64, f64, f64)) -> Self {
        let initial_node = GraphNode {
            position: Vector3::new(start_pos.0, start_pos.1, start_pos.2),
            velocity: Vector3::new(start_vel.0, start_vel.1, start_vel.2),
            accel_bias: Vector3::new(start_bias.0, start_bias.1, start_bias.2),
            gyro_bias: Vector3::zeros(),
            timestamp: 0.0,
        };

        let mut nodes = VecDeque::new();
        nodes.push_back(initial_node);

        Self {
            current_position: Vector3::new(start_pos.0, start_pos.1, start_pos.2),
            current_velocity: Vector3::new(start_vel.0, start_vel.1, start_vel.2),
            current_accel_bias: Vector3::new(start_bias.0, start_bias.1, start_bias.2),
            current_gyro_bias: Vector3::zeros(),
            current_timestamp: 0.0,
            preintegrator: PreintegratedImu::new(),
            imu_queue: VecDeque::new(),
            nodes,
            gps_factors: Vec::new(),
            max_nodes: 100,  // Sliding window size
            gps_noise_std: 8.0,  // meters
            imu_noise_std: 0.05,  // m/s²
            last_optimization_time: 0.0,
            optimization_count: 0,
        }
    }

    /// Fast loop: Enqueue IMU measurement for preintegration (non-blocking)
    pub fn enqueue_imu(&mut self, accel: Vector3<f64>, gyro: Vector3<f64>, timestamp: f64) {
        let dt = if self.current_timestamp > 0.0 {
            timestamp - self.current_timestamp
        } else {
            0.02  // 50Hz default
        };

        if dt > 0.0 && dt < 0.1 {  // Sanity check
            // Bias-corrected measurements
            let accel_corrected = accel - self.current_accel_bias;
            let gyro_corrected = gyro - self.current_gyro_bias;

            // Preintegrate immediately
            self.preintegrator.integrate(accel_corrected, gyro_corrected, dt);

            // Store for potential reoptimization
            self.imu_queue.push_back((accel, gyro, timestamp));
            if self.imu_queue.len() > 1000 {
                self.imu_queue.pop_front();
            }
        }

        self.current_timestamp = timestamp;
    }

    /// Slow loop: Add GPS measurement and trigger optimization
    pub fn add_gps_measurement(&mut self, lat: f64, lon: f64, alt: f64, timestamp: f64) {
        // Convert GPS to local ENU coordinates (simplified - assumes flat Earth)
        let position = Vector3::new(lat * 111320.0, lon * 111320.0 * lat.to_radians().cos(), alt);

        let gps_factor = GpsFactor {
            position,
            timestamp,
            covariance: Matrix3::identity() * self.gps_noise_std * self.gps_noise_std,
        };

        self.gps_factors.push(gps_factor);

        // Create new keyframe node
        self.add_keyframe(timestamp);

        // Trigger optimization
        self.optimize();
    }

    /// Create new keyframe using preintegrated IMU
    fn add_keyframe(&mut self, timestamp: f64) {
        // Predict state using preintegration
        let dt = self.preintegrator.dt_sum;
        if dt < 0.001 {
            return;  // No motion
        }

        let last_node = self.nodes.back().unwrap();

        // Apply preintegrated deltas
        let new_position = last_node.position + self.preintegrator.delta_position;
        let new_velocity = last_node.velocity + self.preintegrator.delta_velocity;

        let new_node = GraphNode {
            position: new_position,
            velocity: new_velocity,
            accel_bias: self.current_accel_bias,
            gyro_bias: self.current_gyro_bias,
            timestamp,
        };

        self.nodes.push_back(new_node);

        // Sliding window
        if self.nodes.len() > self.max_nodes {
            self.nodes.pop_front();
        }

        // Reset preintegrator
        self.preintegrator.reset();
    }

    /// Run graph optimization (Gauss-Newton iteration)
    fn optimize(&mut self) {
        if self.nodes.len() < 2 {
            return;
        }

        // Simplified optimization: just apply GPS correction to latest node
        // Real implementation would use iSAM2 or similar incremental solver

        if let Some(latest_gps) = self.gps_factors.last() {
            if let Some(latest_node) = self.nodes.back_mut() {
                // Weight GPS vs IMU prediction
                let gps_weight = 0.8;  // Trust GPS more
                latest_node.position = latest_node.position * (1.0 - gps_weight)
                    + latest_gps.position * gps_weight;

                // Update current state
                self.current_position = latest_node.position;
                self.current_velocity = latest_node.velocity;
            }
        }

        self.optimization_count += 1;
        self.last_optimization_time = self.current_timestamp;
    }

    /// Get current optimized state
    pub fn get_current_state(&self) -> FgoState {
        FgoState {
            position: [
                self.current_position[0],
                self.current_position[1],
                self.current_position[2],
            ],
            velocity: [
                self.current_velocity[0],
                self.current_velocity[1],
                self.current_velocity[2],
            ],
            accel_bias: [
                self.current_accel_bias[0],
                self.current_accel_bias[1],
                self.current_accel_bias[2],
            ],
            gyro_bias: [
                self.current_gyro_bias[0],
                self.current_gyro_bias[1],
                self.current_gyro_bias[2],
            ],
            timestamp: self.current_timestamp,
        }
    }

    /// Get statistics for debugging
    pub fn get_stats(&self) -> (usize, usize, usize) {
        (self.nodes.len(), self.gps_factors.len(), self.optimization_count)
    }
}
