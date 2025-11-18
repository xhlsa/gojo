use serde::{Deserialize, Serialize};
use std::process::Command;
use tokio::sync::mpsc::Sender;
use tokio::time::{interval, Duration};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct AccelData {
    pub timestamp: f64,
    pub x: f64,
    pub y: f64,
    pub z: f64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct GyroData {
    pub timestamp: f64,
    pub x: f64,
    pub y: f64,
    pub z: f64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct GpsData {
    pub timestamp: f64,
    pub latitude: f64,
    pub longitude: f64,
    pub accuracy: f64,
    pub speed: f64,
}

pub async fn accel_loop(tx: Sender<AccelData>) {
    let mut interval = interval(Duration::from_millis(20)); // ~50Hz sampling
    let mut sample_count = 0u64;

    loop {
        interval.tick().await;

        // Try to read from termux-sensor, fall back to mock data
        let accel = match read_accelerometer() {
            Some(data) => data,
            None => mock_accel_data(),
        };

        match tx.try_send(accel) {
            Ok(_) => {
                sample_count += 1;
                if sample_count % 100 == 0 {
                    eprintln!("[accel] {} samples", sample_count);
                }
            }
            Err(tokio::sync::mpsc::error::TrySendError::Closed(_)) => {
                eprintln!("[accel] Channel closed after {} samples", sample_count);
                break;
            }
            Err(tokio::sync::mpsc::error::TrySendError::Full(_)) => {
                // Channel full, drop this sample
            }
        }
    }
}

pub async fn gyro_loop(tx: Sender<GyroData>, enabled: bool) {
    if !enabled {
        return;
    }

    let mut interval = interval(Duration::from_millis(20)); // ~50Hz sampling
    let mut sample_count = 0u64;

    loop {
        interval.tick().await;

        let gyro = match read_gyroscope() {
            Some(data) => data,
            None => mock_gyro_data(),
        };

        match tx.try_send(gyro) {
            Ok(_) => {
                sample_count += 1;
                if sample_count % 100 == 0 {
                    eprintln!("[gyro] {} samples", sample_count);
                }
            }
            Err(tokio::sync::mpsc::error::TrySendError::Closed(_)) => {
                eprintln!("[gyro] Channel closed after {} samples", sample_count);
                break;
            }
            Err(tokio::sync::mpsc::error::TrySendError::Full(_)) => {
                // Channel full, drop this sample
            }
        }
    }
}

pub async fn gps_loop(tx: Sender<GpsData>) {
    let mut interval = interval(Duration::from_secs(5)); // 0.2 Hz
    let mut sample_count = 0u64;

    loop {
        interval.tick().await;

        // Try to read from LocationAPI, fall back to mock
        let gps = match read_gps() {
            Some(data) => data,
            None => mock_gps_data(),
        };

        match tx.try_send(gps) {
            Ok(_) => {
                sample_count += 1;
                eprintln!("[gps] {} fixes", sample_count);
            }
            Err(tokio::sync::mpsc::error::TrySendError::Closed(_)) => {
                eprintln!("[gps] Channel closed after {} fixes", sample_count);
                break;
            }
            Err(tokio::sync::mpsc::error::TrySendError::Full(_)) => {
                // Channel full, drop this sample
            }
        }
    }
}

fn read_accelerometer() -> Option<AccelData> {
    // Try to read from termux-sensor
    // Format: Accelerometer event: x=X, y=Y, z=Z, accuracy=0, timestamp=TS
    match Command::new("termux-sensor")
        .arg("-n")
        .arg("1")
        .arg("-s")
        .arg("accelerometer")
        .output()
    {
        Ok(output) => {
            let text = String::from_utf8_lossy(&output.stdout);
            parse_accel_output(&text)
        }
        Err(_) => None,
    }
}

fn read_gyroscope() -> Option<GyroData> {
    // Try to read from termux-sensor
    match Command::new("termux-sensor")
        .arg("-n")
        .arg("1")
        .arg("-s")
        .arg("gyroscope")
        .output()
    {
        Ok(output) => {
            let text = String::from_utf8_lossy(&output.stdout);
            parse_gyro_output(&text)
        }
        Err(_) => None,
    }
}

fn read_gps() -> Option<GpsData> {
    // Try to read from termux-location
    // This would need proper JSON parsing from Termux:API
    None
}

fn parse_accel_output(output: &str) -> Option<AccelData> {
    let timestamp = current_timestamp();

    // Example: "Accelerometer event: x=0.5, y=0.3, z=9.8, accuracy=0, timestamp=1234567890"
    let mut x = 0.0;
    let mut y = 0.0;
    let mut z = 0.0;

    for part in output.split(',') {
        if let Some(val_str) = part.strip_prefix("x=") {
            x = val_str.trim().parse().ok()?;
        } else if let Some(val_str) = part.strip_prefix("y=") {
            y = val_str.trim().parse().ok()?;
        } else if let Some(val_str) = part.strip_prefix("z=") {
            z = val_str.trim().parse().ok()?;
        }
    }

    Some(AccelData { timestamp, x, y, z })
}

fn parse_gyro_output(output: &str) -> Option<GyroData> {
    let timestamp = current_timestamp();
    let mut x = 0.0;
    let mut y = 0.0;
    let mut z = 0.0;

    for part in output.split(',') {
        if let Some(val_str) = part.strip_prefix("x=") {
            x = val_str.trim().parse().ok()?;
        } else if let Some(val_str) = part.strip_prefix("y=") {
            y = val_str.trim().parse().ok()?;
        } else if let Some(val_str) = part.strip_prefix("z=") {
            z = val_str.trim().parse().ok()?;
        }
    }

    Some(GyroData { timestamp, x, y, z })
}

fn mock_accel_data() -> AccelData {
    use std::f64::consts::PI;
    static COUNTER: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let t = COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed) as f64 * 0.02;

    AccelData {
        timestamp: current_timestamp(),
        x: (t * 2.0 * PI).sin() * 0.5,
        y: (t * 2.0 * PI).cos() * 0.3,
        z: 9.81 + (t * PI).sin() * 0.1,
    }
}

fn mock_gyro_data() -> GyroData {
    static COUNTER: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let t = COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed) as f64 * 0.02;

    GyroData {
        timestamp: current_timestamp(),
        x: (t * 0.5).sin() * 0.05,
        y: (t * 0.3).cos() * 0.03,
        z: (t * 1.0).sin() * 0.1,
    }
}

fn mock_gps_data() -> GpsData {
    static COUNTER: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let seq = COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed) as f64;

    GpsData {
        timestamp: current_timestamp(),
        latitude: 37.7749 + seq * 0.00001,
        longitude: -122.4194 + seq * 0.00001,
        accuracy: 5.0 + (seq * 0.1).sin() * 2.0,
        speed: 10.0 + (seq * 0.5).sin() * 5.0,
    }
}

fn current_timestamp() -> f64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}
