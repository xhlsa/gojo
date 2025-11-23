use serde::{Deserialize, Serialize};
use std::fs;
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Serialize, Deserialize, Clone)]
pub struct LiveStatus {
    pub timestamp: f64,
    pub accel_samples: u64,
    pub gyro_samples: u64,
    pub gps_fixes: u64,
    pub incidents_detected: u64,
    pub ekf_velocity: f64,
    pub ekf_distance: f64,
    pub ekf_heading_deg: f64,
    pub comp_velocity: f64,
    pub calibration_complete: bool,
    pub gravity_magnitude: f64,
    pub uptime_seconds: u64,
    // GPS data
    pub gps_speed: f64,
    pub gps_bearing: f64,
    pub gps_accuracy: f64,
    pub gps_lat: f64,
    pub gps_lon: f64,
    // Health monitoring
    pub accel_healthy: bool,
    pub gyro_healthy: bool,
    pub gps_healthy: bool,
    pub accel_silence_duration_secs: f64,
    pub gyro_silence_duration_secs: f64,
    pub gps_silence_duration_secs: f64,
    // Restart tracking
    pub accel_restart_count: u32,
    pub gyro_restart_count: u32,
    pub gps_restart_count: u32,
    pub accel_can_restart: bool,
    pub gps_can_restart: bool,
    // Circuit breaker status
    pub circuit_breaker_tripped: bool,
    pub circuit_breaker_since_secs: f64,
    // Virtual dyno (specific power - vehicle-agnostic)
    pub specific_power_w_per_kg: f64,  // Power-to-weight ratio
    pub power_coefficient: f64,         // Normalized power metric
}

impl LiveStatus {
    pub fn new() -> Self {
        Self {
            timestamp: current_timestamp(),
            accel_samples: 0,
            gyro_samples: 0,
            gps_fixes: 0,
            incidents_detected: 0,
            ekf_velocity: 0.0,
            ekf_distance: 0.0,
            ekf_heading_deg: 0.0,
            comp_velocity: 0.0,
            calibration_complete: false,
            gravity_magnitude: 9.81,
            uptime_seconds: 0,
            gps_speed: 0.0,
            gps_bearing: 0.0,
            gps_accuracy: 0.0,
            gps_lat: 0.0,
            gps_lon: 0.0,
            accel_healthy: true,
            gyro_healthy: true,
            gps_healthy: true,
            accel_silence_duration_secs: 0.0,
            gyro_silence_duration_secs: 0.0,
            gps_silence_duration_secs: 0.0,
            accel_restart_count: 0,
            gyro_restart_count: 0,
            gps_restart_count: 0,
            accel_can_restart: true,
            gps_can_restart: true,
            circuit_breaker_tripped: false,
            circuit_breaker_since_secs: 0.0,
            specific_power_w_per_kg: 0.0,
            power_coefficient: 0.0,
        }
    }

    pub fn save(&self, path: &str) -> std::io::Result<()> {
        let json = serde_json::to_string_pretty(self)?;
        fs::write(path, json)?;
        Ok(())
    }
}

pub fn current_timestamp() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}
