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
