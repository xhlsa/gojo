use serde::{Deserialize, Serialize};

/// Accelerometer sample from Android SensorEvent
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AccelSample {
    pub x: f64,
    pub y: f64,
    pub z: f64,
    pub timestamp: f64,
}

impl AccelSample {
    pub fn new(x: f64, y: f64, z: f64, timestamp: f64) -> Self {
        Self { x, y, z, timestamp }
    }

    pub fn magnitude(&self) -> f64 {
        (self.x * self.x + self.y * self.y + self.z * self.z).sqrt()
    }
}

/// Gyroscope sample from Android SensorEvent
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GyroSample {
    pub x: f64,
    pub y: f64,
    pub z: f64,
    pub timestamp: f64,
}

impl GyroSample {
    pub fn new(x: f64, y: f64, z: f64, timestamp: f64) -> Self {
        Self { x, y, z, timestamp }
    }

    pub fn magnitude(&self) -> f64 {
        (self.x * self.x + self.y * self.y + self.z * self.z).sqrt()
    }
}

/// GPS location from Android LocationManager
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GpsSample {
    pub latitude: f64,
    pub longitude: f64,
    pub altitude: f64,
    pub accuracy: f64,
    pub speed: f64,
    pub bearing: f64,
    pub timestamp: f64,
}

impl GpsSample {
    pub fn new(
        latitude: f64,
        longitude: f64,
        altitude: f64,
        accuracy: f64,
        speed: f64,
        bearing: f64,
        timestamp: f64,
    ) -> Self {
        Self {
            latitude,
            longitude,
            altitude,
            accuracy,
            speed,
            bearing,
            timestamp,
        }
    }
}

/// Combined sensor reading for filter processing
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SensorReading {
    pub timestamp: f64,
    pub accel: Option<AccelSample>,
    pub gyro: Option<GyroSample>,
    pub gps: Option<GpsSample>,
}

impl SensorReading {
    pub fn new(timestamp: f64) -> Self {
        Self {
            timestamp,
            accel: None,
            gyro: None,
            gps: None,
        }
    }

    pub fn with_accel(mut self, accel: AccelSample) -> Self {
        self.accel = Some(accel);
        self
    }

    pub fn with_gyro(mut self, gyro: GyroSample) -> Self {
        self.gyro = Some(gyro);
        self
    }

    pub fn with_gps(mut self, gps: GpsSample) -> Self {
        self.gps = Some(gps);
        self
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_accel_magnitude() {
        let accel = AccelSample::new(3.0, 4.0, 0.0, 0.0);
        assert_eq!(accel.magnitude(), 5.0);
    }

    #[test]
    fn test_gyro_magnitude() {
        let gyro = GyroSample::new(0.6, 0.8, 0.0, 0.0);
        assert_eq!(gyro.magnitude(), 1.0);
    }
}
