# Gojo

Real-time GPS/IMU sensor fusion on Android, powered by a 15-state Extended Kalman Filter written in Rust.

## What it does

Runs a 15-dimensional EKF natively on the device via Android NDK/JNI, fusing GPS (1 Hz) and IMU (accelerometer + gyroscope at ~100 Hz) to produce smooth, accurate positioning that maintains tracking through GPS gaps of 100+ seconds. The filtered track is rendered alongside raw GPS on an OpenStreetMap base map so the smoothing effect is immediately visible while driving.

## Architecture

```
Android Sensors  →  Kotlin SensorThread  →  JNI  →  Rust EKF Core  →  Filtered State  →  OSMDroid Map
  (Accel/Gyro          (HandlerThread,           (cdylib,          (@Volatile ref,         (green polyline
   + GPS 1 Hz)          100 Hz / 1 Hz)           no_mangle)         200 ms UI poll)         vs red GPS)
```

**Layer breakdown:**

| Layer | File | Role |
|---|---|---|
| Sensor ingestion | `SensorService.kt` | Foreground service; registers Android sensors on the filter thread's looper |
| Filter thread | `SensorThread.kt` | `HandlerThread` owning the EKF handle; all JNI calls serialized here |
| JNI bridge | `jni.rs` | Thin C-ABI wrapper; exposes init/processImu/processGps/getState/destroy |
| EKF core | `sensor_fusion.rs` | Pure Rust, zero Android dependencies — same code runs on desktop |
| Map display | `MainActivity.kt` | OSMDroid map with 200 ms poll loop; green = EKF, red = raw GPS |
| Session logging | `SessionLogger.kt` | Buffered CSV writer (imu.csv, gps.csv, ekf.csv) for post-drive analysis |

The Rust core is platform-independent: it compiles unchanged for Android (as a `.so` via cargo-ndk) and for Linux/Termux (as a binary with Tokio + axum).

## Filter states

The 15D EKF tracks:

| Index | State | Dimension |
|---|---|---|
| 0–2 | Position (ENU, metres from origin) | 3 |
| 3–5 | Velocity (m/s) | 3 |
| 6–9 | Orientation quaternion | 4 |
| 10–12 | Accelerometer bias (m/s²) | 3 |
| 13–14 | Gyroscope bias (rad/s) | 2 |

Tracking orientation and sensor biases jointly means the filter can correct for phone mounting angle and drift in real time — critical for dead reckoning during GPS outages.

## Key features

- Sub-metre accuracy in open sky (validated: 1.17 m RMSE)
- Dead reckoning through 100+ second GPS gaps (4.6 m RMSE at 10× decimation)
- Stationary detection (ZUPT) with automatic zero-velocity updates
- Driving incident detection (braking / swerve / impact)
- Road surface roughness estimation
- Per-session CSV logging (imu.csv, gps.csv, ekf.csv) compatible with Python analysis tools
- No API keys — OpenStreetMap tiles via OSMDroid
- One-tap calibration screen before each drive

## Screenshots / Demo

<!-- Screenshots and demo video coming soon — screen recording of a drive showing the green (EKF) and red (raw GPS) polylines -->

## Build

### Prerequisites

```bash
# Rust toolchain
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
rustup target add aarch64-linux-android

# cargo-ndk (Android cross-compilation helper)
cargo install cargo-ndk

# Android NDK — install via Android Studio → SDK Manager → SDK Tools → NDK
export ANDROID_NDK_HOME=$HOME/Android/Sdk/ndk/27.x.xxxxxxx
```

### Compile and install

```bash
# 1. Build the Rust .so and copy it into the Android project
./build.sh

# 2. Open android/ in Android Studio and run on device
#    (or: cd android && ./gradlew installDebug)
```

No API keys, no Play Services, no Google account required.

## Project structure

```
gojo/
├── build.sh                          # cargo-ndk wrapper → copies .so to jniLibs/
├── motion_tracker_rs/                # Rust workspace
│   └── src/
│       ├── sensor_fusion.rs          # EKF core — platform-independent
│       ├── jni.rs                    # Android JNI bridge (cfg android only)
│       ├── ekf_15d.rs                # 15D filter implementation
│       └── main.rs                   # Termux binary (cfg not android)
└── android/                          # Android Studio project
    └── app/src/main/java/com/gojo/
        ├── core/
        │   ├── GojoJni.kt            # external fun declarations + FLAG_* constants
        │   ├── SensorThread.kt       # filter thread — owns EKF handle + logger
        │   ├── SensorService.kt      # foreground service — sensor registration
        │   ├── SessionLogger.kt      # buffered CSV writer
        │   ├── CalibrationActivity.kt# stationary calibration before each drive
        │   └── GojoApp.kt            # Application subclass — calibration holder
        └── logger/
            └── MainActivity.kt       # OSMDroid map + HUD
```

## Replay / offline analysis

The Termux workflow and the desktop `replay` binary still work for analyzing logged sessions:

```bash
# Replay a saved session and print RMSE metrics
cd motion_tracker_rs
cargo run --bin replay -- --log ../sessions/comparison_YYYYMMDD.json.gz

# Verify CSV alignment from an Android session
python android/tools/verify_data.py /path/to/Gojo/sessions/YYYY-MM-DD_HH-mm-ss/
```

Session folders are written to `Android/data/com.gojo.logger/files/Gojo/sessions/` on the device.
Pull via `adb pull /sdcard/Android/data/com.gojo.logger/files/Gojo/sessions/ ./sessions/`.

## License

MIT
