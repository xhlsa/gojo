use std::sync::{Arc, Mutex};
use tokio::time::{sleep, Duration, Instant};

/// Tracks health metrics for individual sensors
#[derive(Clone, Debug)]
pub struct SensorHealth {
    pub name: String,
    pub last_update: Arc<Mutex<Instant>>,
    pub silence_threshold: Duration,
    pub max_restart_attempts: u32,
    pub restart_attempts: Arc<Mutex<u32>>,
}

impl SensorHealth {
    pub fn new(name: &str, silence_threshold_secs: u64, max_restarts: u32) -> Self {
        SensorHealth {
            name: name.to_string(),
            last_update: Arc::new(Mutex::new(Instant::now())),
            silence_threshold: Duration::from_secs(silence_threshold_secs),
            max_restart_attempts: max_restarts,
            restart_attempts: Arc::new(Mutex::new(0)),
        }
    }

    pub fn update(&self) {
        if let Ok(mut time) = self.last_update.lock() {
            *time = Instant::now();
        }
    }

    pub fn time_since_last_update(&self) -> Option<Duration> {
        self.last_update.lock().ok().map(|t| t.elapsed())
    }

    pub fn is_silent(&self) -> bool {
        self.time_since_last_update()
            .map(|d| d > self.silence_threshold)
            .unwrap_or(false)
    }

    pub fn can_restart(&self) -> bool {
        self.restart_attempts
            .lock()
            .ok()
            .map(|r| *r < self.max_restart_attempts)
            .unwrap_or(false)
    }

    pub fn increment_restart_attempts(&self) {
        if let Ok(mut attempts) = self.restart_attempts.lock() {
            *attempts += 1;
        }
    }

    pub fn reset_restart_attempts(&self) {
        if let Ok(mut attempts) = self.restart_attempts.lock() {
            *attempts = 0;
        }
    }

    pub fn get_restart_attempts(&self) -> u32 {
        self.restart_attempts
            .lock()
            .ok()
            .map(|r| *r)
            .unwrap_or(0)
    }
}

/// Health monitor task that periodically checks sensor status
pub struct HealthMonitor {
    pub accel: SensorHealth,
    pub gyro: SensorHealth,
    pub gps: SensorHealth,
    check_interval: Duration,
}

impl HealthMonitor {
    pub fn new() -> Self {
        // Thresholds from CLAUDE.md:
        // - Accel silence: 5 seconds
        // - GPS silence: 30 seconds
        // - Gyro: paired with accel, no separate check needed
        HealthMonitor {
            accel: SensorHealth::new("Accel", 5, 60),
            gyro: SensorHealth::new("Gyro", 5, 60),
            gps: SensorHealth::new("GPS", 30, 60),
            check_interval: Duration::from_secs(2), // Check every 2 seconds
        }
    }

    /// Check all sensors and report health status
    pub fn check_health(&self) -> HealthReport {
        HealthReport {
            accel_healthy: !self.accel.is_silent(),
            accel_silence_duration: self.accel.time_since_last_update(),
            accel_can_restart: self.accel.can_restart(),
            accel_restart_count: self.accel.get_restart_attempts(),

            gyro_healthy: !self.gyro.is_silent(),
            gyro_silence_duration: self.gyro.time_since_last_update(),
            gyro_restart_count: self.gyro.get_restart_attempts(),

            gps_healthy: !self.gps.is_silent(),
            gps_silence_duration: self.gps.time_since_last_update(),
            gps_can_restart: self.gps.can_restart(),
            gps_restart_count: self.gps.get_restart_attempts(),
        }
    }

    /// Format health status for logging
    pub fn format_status(&self) -> String {
        let report = self.check_health();

        let accel_status = if report.accel_healthy {
            "✓".to_string()
        } else {
            format!(
                "⚠ (silent {:.1}s)",
                report
                    .accel_silence_duration
                    .unwrap_or(Duration::from_secs(0))
                    .as_secs_f64()
            )
        };

        let gyro_status = if report.gyro_healthy {
            "✓".to_string()
        } else {
            format!(
                "⚠ (silent {:.1}s)",
                report
                    .gyro_silence_duration
                    .unwrap_or(Duration::from_secs(0))
                    .as_secs_f64()
            )
        };

        let gps_status = if report.gps_healthy {
            "✓".to_string()
        } else {
            format!(
                "⚠ (silent {:.1}s)",
                report
                    .gps_silence_duration
                    .unwrap_or(Duration::from_secs(0))
                    .as_secs_f64()
            )
        };

        format!(
            "Health: Accel {} | Gyro {} | GPS {}",
            accel_status, gyro_status, gps_status
        )
    }
}

impl Default for HealthMonitor {
    fn default() -> Self {
        Self::new()
    }
}

/// Report of sensor health status
pub struct HealthReport {
    pub accel_healthy: bool,
    pub accel_silence_duration: Option<Duration>,
    pub accel_can_restart: bool,
    pub accel_restart_count: u32,

    pub gyro_healthy: bool,
    pub gyro_silence_duration: Option<Duration>,
    pub gyro_restart_count: u32,

    pub gps_healthy: bool,
    pub gps_silence_duration: Option<Duration>,
    pub gps_can_restart: bool,
    pub gps_restart_count: u32,
}

/// Spawn health monitoring task
pub async fn health_monitor_task(monitor: Arc<HealthMonitor>) {
    loop {
        sleep(monitor.check_interval).await;

        let report = monitor.check_health();

        // Log warnings for silent sensors
        if !report.accel_healthy && report.accel_can_restart {
            if let Some(duration) = report.accel_silence_duration {
                eprintln!(
                    "[HEALTH] ⚠️ Accel SILENT for {:.1}s (restart attempt {}/{})",
                    duration.as_secs_f64(),
                    report.accel_restart_count,
                    monitor.accel.max_restart_attempts
                );
                monitor.accel.increment_restart_attempts();
            }
        }

        if !report.gps_healthy && report.gps_can_restart {
            if let Some(duration) = report.gps_silence_duration {
                eprintln!(
                    "[HEALTH] ⚠️ GPS SILENT for {:.1}s (restart attempt {}/{})",
                    duration.as_secs_f64(),
                    report.gps_restart_count,
                    monitor.gps.max_restart_attempts
                );
                monitor.gps.increment_restart_attempts();
            }
        }

        // Log max restart attempts exceeded
        if !report.accel_healthy && !report.accel_can_restart {
            eprintln!(
                "[HEALTH] ✗ Accel DEAD - max restart attempts exceeded, continuing without accel"
            );
        }

        if !report.gps_healthy && !report.gps_can_restart {
            eprintln!(
                "[HEALTH] ✗ GPS DEAD - max restart attempts exceeded, continuing without GPS"
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::thread;

    #[test]
    fn test_sensor_health_detection() {
        let health = SensorHealth::new("test", 1, 3);

        // Initially healthy (just created)
        assert!(!health.is_silent());

        // Wait for silence threshold
        thread::sleep(Duration::from_millis(1100));

        // Now should be silent
        assert!(health.is_silent());

        // Update should reset
        health.update();
        assert!(!health.is_silent());
    }

    #[test]
    fn test_restart_attempts() {
        let health = SensorHealth::new("test", 10, 3);

        assert_eq!(health.get_restart_attempts(), 0);
        assert!(health.can_restart());

        health.increment_restart_attempts();
        assert_eq!(health.get_restart_attempts(), 1);

        health.increment_restart_attempts();
        health.increment_restart_attempts();
        assert_eq!(health.get_restart_attempts(), 3);
        assert!(!health.can_restart());
    }

    #[test]
    fn test_health_monitor() {
        let monitor = HealthMonitor::new();

        let report = monitor.check_health();
        assert!(report.accel_healthy);
        assert!(report.gps_healthy);

        let status = monitor.format_status();
        assert!(status.contains("✓"));
    }
}
