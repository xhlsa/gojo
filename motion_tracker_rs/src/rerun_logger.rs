use anyhow::Result;
use rerun::{archetypes::Scalar, RecordingStreamBuilder};

/// Rerun 3D visualization logger for motion tracking and high-speed replay
/// Supports Rerun v0.15+ API with archetype-based logging
pub struct RerunLogger {
    rec: rerun::RecordingStream,
}

impl RerunLogger {
    /// Initialize Rerun recording to file
    /// Takes output path (e.g., "motion_tracker_sessions/rerun_20251122_120000.rrd")
    pub fn new(output_path: &str) -> Result<Self> {
        let rec = RecordingStreamBuilder::new("gojo_drive_log")
            .save(output_path)
            .map_err(|e| anyhow::anyhow!("Failed to create Rerun recording: {}", e))?;

        eprintln!("[RERUN] Recording initialized to: {}", output_path);

        Ok(RerunLogger { rec })
    }

    /// Set the current time for all subsequent logs
    pub fn set_time(&self, elapsed_secs: f64) {
        self.rec.set_time_seconds("stable_time", elapsed_secs);
    }

    /// Log a scalar value (generic for any measurement)
    pub fn log_scalar(&self, path: &str, value: f64) {
        // Rerun v0.15+: Use archetype pattern with f64 directly
        let _ = self.rec.log(path, &Scalar::new(value));
    }

    /// Log GPS data (speed, altitude, position)
    pub fn log_gps(&self, latitude: f64, longitude: f64, _altitude: f64, speed: f64) {
        self.log_scalar("sensors/gps/speed", speed);
        self.log_scalar("sensors/gps/latitude", latitude);
        self.log_scalar("sensors/gps/longitude", longitude);
    }


    /// Log raw accelerometer data (time-series)
    pub fn log_accel_raw(&self, x: f64, y: f64, z: f64) {
        self.log_scalar("physics/accel/raw_x", x);
        self.log_scalar("physics/accel/raw_y", y);
        self.log_scalar("physics/accel/raw_z", z);
    }

    /// Log filtered accelerometer data (gravity-corrected)
    pub fn log_accel_filtered(&self, x: f64, y: f64, z: f64) {
        self.log_scalar("physics/accel/filtered_x", x);
        self.log_scalar("physics/accel/filtered_y", y);
        self.log_scalar("physics/accel/filtered_z", z);
    }

    /// Log raw gyroscope data
    pub fn log_gyro_raw(&self, x: f64, y: f64, z: f64) {
        self.log_scalar("physics/gyro/raw_x", x);
        self.log_scalar("physics/gyro/raw_y", y);
        self.log_scalar("physics/gyro/raw_z", z);
    }

    /// Log EKF filter state (velocity, heading, etc)
    pub fn log_ekf_velocity(&self, vx: f64, vy: f64, vz: f64) {
        let speed = (vx * vx + vy * vy + vz * vz).sqrt();
        self.log_scalar("filter/ekf/velocity_x", vx);
        self.log_scalar("filter/ekf/velocity_y", vy);
        self.log_scalar("filter/ekf/velocity_z", vz);
        self.log_scalar("filter/ekf/speed", speed);
    }


    /// Log incident detection event
    pub fn log_incident(&self, incident_type: &str, magnitude: f64, latitude: f64, longitude: f64) {
        let path = format!("incidents/{}", incident_type);
        self.log_scalar(&path, magnitude);
        self.log_scalar("incidents/location_lat", latitude);
        self.log_scalar("incidents/location_lon", longitude);
    }

}
