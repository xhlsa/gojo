# Gojo Logger

Bare-bones Android APK for collecting time-aligned GPS + IMU data.

## Why This Exists

Android's sensor pipeline doesn't guarantee timestamp alignment between GPS and
IMU when accessed through Termux or other indirect methods. This app hits the
hardware APIs directly and uses `elapsedRealtimeNanos` as a shared clock base
for both sensor streams.

## Data Format

Each logging session creates a timestamped folder in:
```
Android/data/com.gojo.logger/files/Documents/gojo_YYYYMMDD_HHmmss/
```

### `imu.csv`
| Column | Description |
|--------|-------------|
| elapsed_nanos | elapsedRealtimeNanos (ns since boot) |
| sensor_type | `accel` or `gyro` |
| x, y, z | Sensor values (m/s² for accel, rad/s for gyro) |
| accuracy | Android accuracy flag (0-3) |

### `gps.csv`
| Column | Description |
|--------|-------------|
| elapsed_nanos | elapsedRealtimeNanos (ns since boot) |
| lat, lon | WGS84 coordinates |
| alt_m | Altitude in meters |
| accuracy_m | Horizontal accuracy in meters |
| speed_mps | Speed in m/s |
| bearing_deg | Bearing in degrees |
| satellites | Satellite count (-1 if unavailable) |

### `metadata.txt`
Contains session info including:
- `boot_time_utc_ms`: UTC milliseconds at device boot. Use this to convert
  `elapsed_nanos` to wall clock time:
  `utc_ms = boot_time_utc_ms + (elapsed_nanos / 1_000_000)`

## Timestamp Alignment

**Both CSV files use the same clock: `SystemClock.elapsedRealtimeNanos()`**

- IMU: `SensorEvent.timestamp` (elapsedRealtimeNanos on API 26+)
- GPS: `Location.getElapsedRealtimeNanos()`

To align in post-processing, just merge on the `elapsed_nanos` column.
No clock domain conversion needed.

## Configuration

In `LoggingService.kt`:
- `IMU_PERIOD_US = 10_000` → ~100Hz IMU sampling
- `GPS_INTERVAL_MS = 1000` → 1Hz GPS

Adjust as needed for your use case.

## Building

1. Open in Android Studio
2. Sync Gradle
3. Build → Run on device
4. Grant location permissions when prompted
5. Hit START LOGGING
6. Pull data via USB or `adb pull`

## Pulling Data

```bash
adb shell ls /sdcard/Android/data/com.gojo.logger/files/Documents/
adb pull /sdcard/Android/data/com.gojo.logger/files/Documents/gojo_XXXXXXXX_XXXXXX/ ./data/
```
