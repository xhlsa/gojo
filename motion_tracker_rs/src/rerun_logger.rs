use anyhow::Result;
use rerun::{
    archetypes::Scalar,
    RecordingStreamBuilder,
};

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

    /// Log 3D vehicle orientation (quaternion) - placeholder for future Transform3D
    pub fn log_orientation(&self, qw: f64, qx: f64, qy: f64, qz: f64) {
        // Store quaternion components as scalars for now
        self.log_scalar("world/vehicle/qw", qw);
        self.log_scalar("world/vehicle/qx", qx);
        self.log_scalar("world/vehicle/qy", qy);
        self.log_scalar("world/vehicle/qz", qz);
    }

    /// Log 3D vehicle position (in local frame)
    pub fn log_position(&self, x: f64, y: f64, z: f64) {
        self.log_scalar("world/vehicle_position/x", x);
        self.log_scalar("world/vehicle_position/y", y);
        self.log_scalar("world/vehicle_position/z", z);
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

    /// Log 13D filter state (experimental shadow mode)
    pub fn log_13d_state(
        &self,
        pos_x: f64,
        pos_y: f64,
        pos_z: f64,
        vel_x: f64,
        vel_y: f64,
        vel_z: f64,
        qw: f64,
        qx: f64,
        qy: f64,
        qz: f64,
    ) {
        // Position in 13D frame
        self.log_scalar("filter/ekf_13d/position_x", pos_x);
        self.log_scalar("filter/ekf_13d/position_y", pos_y);
        self.log_scalar("filter/ekf_13d/position_z", pos_z);

        // Velocity in 13D frame
        let speed_13d = (vel_x * vel_x + vel_y * vel_y + vel_z * vel_z).sqrt();
        self.log_scalar("filter/ekf_13d/velocity_x", vel_x);
        self.log_scalar("filter/ekf_13d/velocity_y", vel_y);
        self.log_scalar("filter/ekf_13d/velocity_z", vel_z);
        self.log_scalar("filter/ekf_13d/speed", speed_13d);

        // Quaternion orientation
        self.log_scalar("filter/ekf_13d/orientation_qw", qw);
        self.log_scalar("filter/ekf_13d/orientation_qx", qx);
        self.log_scalar("filter/ekf_13d/orientation_qy", qy);
        self.log_scalar("filter/ekf_13d/orientation_qz", qz);
    }

    /// Log incident detection event
    pub fn log_incident(&self, incident_type: &str, magnitude: f64, latitude: f64, longitude: f64) {
        let path = format!("incidents/{}", incident_type);
        self.log_scalar(&path, magnitude);
        self.log_scalar("incidents/location_lat", latitude);
        self.log_scalar("incidents/location_lon", longitude);
    }

    /// Log comparison metric (8D vs 13D)
    pub fn log_filter_comparison(&self, metric_name: &str, value_8d: f64, value_13d: f64) {
        let path_8d = format!("comparison/{}/8d", metric_name);
        let path_13d = format!("comparison/{}/13d", metric_name);
        self.log_scalar(&path_8d, value_8d);
        self.log_scalar(&path_13d, value_13d);
    }
}
