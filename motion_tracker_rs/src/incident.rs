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
        // Impact: accel > 1.5g (highest severity, check first)
        if accel_mag > 1.5 {
            return Some(Incident {
                timestamp,
                incident_type: "impact".to_string(),
                magnitude: accel_mag,
                gps_speed,
                latitude: lat,
                longitude: lon,
            });
        }

        // Hard braking: accel > 0.8g
        if accel_mag > 0.8 {
            return Some(Incident {
                timestamp,
                incident_type: "hard_braking".to_string(),
                magnitude: accel_mag,
                gps_speed,
                latitude: lat,
                longitude: lon,
            });
        }

        // Swerving: gyro_z > 60°/sec with GPS speed > 2 m/s
        let gyro_thresh = 60.0 * std::f64::consts::PI / 180.0; // Convert to rad/s
        if gyro_z.abs() > gyro_thresh {
            if let Some(speed) = gps_speed {
                if speed > 2.0 {
                    // Apply cooldown
                    if (timestamp - self.last_swerve_time) >= self.swerve_cooldown {
                        self.last_swerve_time = timestamp;
                        return Some(Incident {
                            timestamp,
                            incident_type: "swerving".to_string(),
                            magnitude: gyro_z.to_degrees(),
                            gps_speed: Some(speed),
                            latitude: lat,
                            longitude: lon,
                        });
                    }
                }
            }
        }

        None
    }
}
