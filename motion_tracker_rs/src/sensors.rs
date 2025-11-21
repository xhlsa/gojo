use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::io::{BufReader, Read};
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};
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
    pub bearing: f64,
}

// Shared IMU stream - single termux-sensor process outputs both accel and gyro
// This is spawned once in accel_loop and referenced by gyro_loop via Arc<Mutex>
static SHARED_IMU_INITIALIZED: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

pub async fn accel_loop(tx: Sender<AccelData>) {
    eprintln!("[accel] STARTUP: Initializing accelerometer loop");

    // CRITICAL: Cleanup sensor before starting to ensure accelerometer works
    // Without this, accel may produce no output (Termux API quirk)
    let cleanup_result = Command::new("termux-sensor")
        .arg("-c")
        .output();
    eprintln!("[accel] STARTUP: Sensor cleanup complete: {:?}, waiting 500ms for backend reset...", cleanup_result.is_ok());

    // Wait for sensor backend to fully reset after cleanup
    tokio::time::sleep(tokio::time::Duration::from_millis(500)).await;

    // Start persistent termux-sensor process for accelerometer (match gyro pattern exactly)
    eprintln!("[accel] STARTUP: Spawning termux-sensor process...");
    let mut sensor_proc = match Command::new("termux-sensor")
        .arg("-d")
        .arg("10")  // 10ms delay = ~100Hz sampling (batch drained every 10ms)
        .arg("-s")
        .arg("lsm6dso LSM6DSO Accelerometer Non-wakeup")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
    {
        Ok(p) => {
            eprintln!("[accel] STARTUP: ✓ Process spawned successfully");
            SHARED_IMU_INITIALIZED.store(true, std::sync::atomic::Ordering::Relaxed);
            p
        }
        Err(e) => {
            eprintln!("[accel] STARTUP: Failed to spawn termux-sensor: {}", e);
            eprintln!("[accel] STARTUP: Falling back to mock data");
            mock_accel_loop(tx).await;
            return;
        }
    };

    let stdout = match sensor_proc.stdout.take() {
        Some(s) => {
            eprintln!("[accel] STARTUP: ✓ Got stdout, starting to read lines");
            s
        }
        None => {
            eprintln!("[accel] STARTUP: No stdout from termux-sensor");
            return;
        }
    };

    let reader = BufReader::new(stdout);
    let mut sample_count = 0u64;

    eprintln!("[accel] STARTUP: Starting streaming JSON deserializer");
    let stream = serde_json::Deserializer::from_reader(reader).into_iter::<Value>();
    eprintln!("[accel] STARTUP: Deserializer created, entering stream loop");

    for value_result in stream {
        match value_result {
            Ok(data) => {
                if let Some(obj) = data.as_object() {
                    // Skip empty {} objects (termux-sensor warmup phase)
                    if obj.is_empty() {
                        continue;
                    }

                    for (sensor_key, sensor_data) in obj.iter() {
                        // Check if this is accelerometer data
                        if sensor_key.contains("Accelerometer") {
                            if let Some(values) = sensor_data.get("values").and_then(|v| v.as_array()) {
                                if values.len() >= 3 {
                                    let accel = AccelData {
                                        timestamp: current_timestamp(),
                                        x: values[0].as_f64().unwrap_or(0.0),
                                        y: values[1].as_f64().unwrap_or(0.0),
                                        z: values[2].as_f64().unwrap_or(0.0),
                                    };

                                    match tx.try_send(accel) {
                                        Ok(_) => {
                                            sample_count += 1;
                                            if sample_count % 100 == 0 {
                                                eprintln!("[accel] {} samples SENT to channel", sample_count);
                                            }
                                        }
                                        Err(tokio::sync::mpsc::error::TrySendError::Closed(_)) => {
                                            eprintln!("[accel] Channel closed after {} samples", sample_count);
                                            return;
                                        }
                                        Err(tokio::sync::mpsc::error::TrySendError::Full(_)) => {
                                            // Channel full, drop sample
                                            if sample_count == 500 {
                                                eprintln!("[accel] *** CHANNEL FULL at 500 samples, dropping future samples ***");
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
            Err(e) => {
                eprintln!("[accel] JSON error: {}", e);
                break;
            }
        }
    }

    eprintln!("[accel] Stream ended after {} samples", sample_count);
}

async fn mock_accel_loop(tx: Sender<AccelData>) {
    let mut interval = interval(Duration::from_millis(20));
    let mut sample_count = 0u64;

    loop {
        interval.tick().await;

        let accel = mock_accel_data();
        match tx.try_send(accel) {
            Ok(_) => {
                sample_count += 1;
                if sample_count % 100 == 0 {
                    eprintln!("[accel-mock] {} samples", sample_count);
                }
            }
            Err(tokio::sync::mpsc::error::TrySendError::Closed(_)) => {
                eprintln!("[accel-mock] Channel closed after {} samples", sample_count);
                break;
            }
            Err(tokio::sync::mpsc::error::TrySendError::Full(_)) => {}
        }
    }
}

pub async fn gyro_loop(tx: Sender<GyroData>, enabled: bool) {
    if !enabled {
        return;
    }

    // Gyroscope shares the IMU stream with accelerometer
    // Instead of spawning a separate termux-sensor process, we read from the shared IMU stream
    // via the SAME JSON output that accel_loop reads from.
    //
    // LIMITATION: This simple approach requires accel_loop to run first and forward gyro data.
    // Since accel_loop already reads the complete JSON objects and knows about both sensors,
    // the proper fix would be to have a shared message channel or parser.
    // For now, we spawn independent termux-sensor with explicit gyro sensor ID (fallback approach).

    // Note: Gyro sensor cleanup is NOT needed here - accel_loop already does cleanup at startup
    // Running cleanup twice causes race conditions and stream termination
    eprintln!("[gyro] Starting gyro loop (skipping cleanup - accel already cleaned)");

    let mut sensor_proc = match Command::new("termux-sensor")
        .arg("-d")
        .arg("10")  // 10ms delay = ~100Hz sampling (batch drained every 10ms)
        .arg("-s")
        .arg("lsm6dso LSM6DSO Gyroscope Non-wakeup")
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
    {
        Ok(p) => p,
        Err(_) => {
            eprintln!("[gyro] Failed to spawn termux-sensor, falling back to mock data");
            mock_gyro_loop(tx).await;
            return;
        }
    };

    let stdout = match sensor_proc.stdout.take() {
        Some(s) => s,
        None => {
            eprintln!("[gyro] No stdout from termux-sensor");
            return;
        }
    };

    let reader = BufReader::new(stdout);
    let mut sample_count = 0u64;

    let stream = serde_json::Deserializer::from_reader(reader).into_iter::<Value>();

    for value_result in stream {
        match value_result {
            Ok(data) => {
                if let Some(obj) = data.as_object() {
                    // Skip empty {} objects (termux-sensor warmup phase)
                    if obj.is_empty() {
                        continue;
                    }

                    for (sensor_key, sensor_data) in obj.iter() {
                        // Check if this is gyroscope data
                        if sensor_key.contains("Gyroscope") {
                            if let Some(values) = sensor_data.get("values").and_then(|v| v.as_array()) {
                                if values.len() >= 3 {
                                    let gyro = GyroData {
                                        timestamp: current_timestamp(),
                                        x: values[0].as_f64().unwrap_or(0.0),
                                        y: values[1].as_f64().unwrap_or(0.0),
                                        z: values[2].as_f64().unwrap_or(0.0),
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
                                            return;
                                        }
                                        Err(tokio::sync::mpsc::error::TrySendError::Full(_)) => {
                                            // Channel full, drop sample
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
            Err(e) => {
                eprintln!("[gyro] JSON error: {}", e);
                break;
            }
        }
    }

    eprintln!("[gyro] Stream ended after {} samples", sample_count);
}

async fn mock_gyro_loop(tx: Sender<GyroData>) {
    let mut interval = interval(Duration::from_millis(20));
    let mut sample_count = 0u64;

    loop {
        interval.tick().await;

        let gyro = mock_gyro_data();
        match tx.try_send(gyro) {
            Ok(_) => {
                sample_count += 1;
                if sample_count % 100 == 0 {
                    eprintln!("[gyro-mock] {} samples", sample_count);
                }
            }
            Err(tokio::sync::mpsc::error::TrySendError::Closed(_)) => {
                eprintln!("[gyro-mock] Channel closed after {} samples", sample_count);
                break;
            }
            Err(tokio::sync::mpsc::error::TrySendError::Full(_)) => {}
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


fn read_gps() -> Option<GpsData> {
    let output = Command::new("termux-location")
        .arg("-p")
        .arg("gps")
        .output()
        .ok()?;

    if !output.status.success() {
        return None;
    }

    let data: Value = serde_json::from_slice(&output.stdout).ok()?;
    let latitude = data.get("latitude")?.as_f64()?;
    let longitude = data.get("longitude")?.as_f64()?;
    let accuracy = data.get("accuracy").and_then(|v| v.as_f64()).unwrap_or(5.0);
    let speed = data.get("speed").and_then(|v| v.as_f64()).unwrap_or(0.0);
    let timestamp = data
        .get("time")
        .and_then(|v| v.as_f64())
        .unwrap_or_else(current_timestamp);

    Some(GpsData {
        timestamp,
        latitude,
        longitude,
        accuracy,
        speed,
        bearing: 0.0,
    })
}

fn parse_accel_output(output: &str) -> Option<AccelData> {
    if let Some((timestamp, x, y, z)) = parse_sensor_json(output, "x", "y", "z") {
        return Some(AccelData { timestamp, x, y, z });
    }

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

    Some(AccelData { timestamp, x, y, z })
}

fn parse_gyro_output(output: &str) -> Option<GyroData> {
    if let Some((timestamp, x, y, z)) = parse_sensor_json(output, "x", "y", "z") {
        return Some(GyroData { timestamp, x, y, z });
    }

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
        bearing: (seq * 10.0) % 360.0,
    }
}

fn current_timestamp() -> f64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

fn parse_sensor_json(output: &str, _x_key: &str, _y_key: &str, _z_key: &str) -> Option<(f64, f64, f64, f64)> {
    // Termux-sensor format: {"lsm6dso LSM6DSO Accelerometer Non-wakeup": {"values": [x, y, z]}}
    let value: Value = serde_json::from_str(output).ok()?;

    // Get first sensor entry (there's usually only one)
    let sensor_data = value.as_object()?.values().next()?;
    let values = sensor_data.get("values")?.as_array()?;

    // Parse [x, y, z] array
    if values.len() != 3 {
        return None;
    }

    let x = values[0].as_f64()?;
    let y = values[1].as_f64()?;
    let z = values[2].as_f64()?;
    let timestamp = current_timestamp();

    Some((timestamp, x, y, z))
}
