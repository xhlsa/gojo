use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Incident {
    pub timestamp: f64,
    pub incident_type: String,  // "braking", "impact", "swerving"
    pub magnitude: f64,         // g-force or deg/sec
    pub gps_speed: Option<f64>, // m/s
    pub latitude: Option<f64>,
    pub longitude: Option<f64>,
}

pub struct IncidentDetector {
    last_swerve_time: f64,
    swerve_cooldown: f64, // 5 seconds
}

impl IncidentDetector {
    pub fn new() -> Self {
        Self {
            last_swerve_time: 0.0,
            swerve_cooldown: 5.0,
        }
    }

    pub fn detect(
        &mut self,
        accel_mag: f64,
        gyro_z: f64,
        gps_speed: Option<f64>,
        timestamp: f64,
        lat: Option<f64>,
        lon: Option<f64>,
    ) -> Option<Incident> {
        // Thresholds (aligned with main.rs)
        let crash_threshold = 20.0; // m/s^2
        let hard_maneuver_threshold = 4.0; // m/s^2
        let swerve_threshold_deg = 45.0; // deg/s

        // Impact: > 20 m/s^2 (highest severity, check first)
        if accel_mag > crash_threshold {
            return Some(Incident {
                timestamp,
                incident_type: "impact".to_string(),
                magnitude: accel_mag,
                gps_speed,
                latitude: lat,
                longitude: lon,
            });
        }

        // Hard Maneuver (Braking/Turn): > 4.0 m/s^2 (use raw dynamics, no speed gate)
        if accel_mag > hard_maneuver_threshold {
            return Some(Incident {
                timestamp,
                incident_type: "hard_maneuver".to_string(),
                magnitude: accel_mag,
                gps_speed,
                latitude: lat,
                longitude: lon,
            });
        }

        // Swerving: gyro_z > 45°/sec (no speed gate, still apply cooldown)
        let gyro_thresh_rad = swerve_threshold_deg * std::f64::consts::PI / 180.0;
        if gyro_z.abs() > gyro_thresh_rad {
            if (timestamp - self.last_swerve_time) >= self.swerve_cooldown {
                self.last_swerve_time = timestamp;
                return Some(Incident {
                    timestamp,
                    incident_type: "swerving".to_string(),
                    magnitude: gyro_z.to_degrees(),
                    gps_speed,
                    latitude: lat,
                    longitude: lon,
                });
            }
        }

        None
    }
}
