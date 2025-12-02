# Gojo - Android Phone Sensor Fusion

A lean, on-device sensor-fusion logger for Android/Termux. It ingests your phone's accelerometer, gyroscope, GPS (and optional magnetometer/barometer), runs a **15D Extended Kalman Filter** in Rust, and spits out comparison logs you can replay offline. Built to be hackable, measurable, and friendly to low-power phones.

## Key Features

- **Termux-first**: No Play Store deps; uses `termux-sensor` + `termux-location` directly.
- **Rust core**: 15D EKF with full cross-covariance GPS updates; replayable for A/B tuning.
- **Transparent data**: Every run saves a `comparison_*.json.gz` with raw sensors, EKF states, incidents, and metrics.
- **Validated performance**: 
  - **1.17m RMSE** position accuracy with full GPS coverage
  - **4.61m RMSE** at 10x GPS decimation (90% denial, ~100s gaps)
  - **5.75m RMSE** at 20x GPS decimation (95% denial, ~120s gaps)
  - Sub-meter dead reckoning through typical tunnel/canyon scenarios

## Sensor Requirements

- **Required**: Accelerometer, Gyroscope, GPS
- **Optional** (off by default): 
  - Magnetometer yaw assist (`--enable-mag`)
  - Barometer vertical damping (`--enable-baro`)
- **Gating**: 
  - Mag only when speed > 5 m/s and GPS gap > 3s
  - Baro only when last GPS speed > 3 m/s

## Key Capabilities

### Non-Holonomic Constraints (NHC)
- Auto-calibrated phone mounting offset (typically ±30°)
- Constrains lateral velocity during motion (speed > 2.5 m/s)
- Significantly improves dead reckoning through GPS gaps
- Disabled during GPS gaps > 3s (heading uncertainty grows)

### GPS Gap Handling
- Speed gating prevents velocity explosions during long gaps
- Tested up to 120-second GPS blackouts with graceful degradation
- Cross-covariance propagation maintains filter consistency
- Heading-aided gyro bias estimation during straight driving

### Dead Reckoning Budget
Real-world validated performance during GPS denial:

| GPS Coverage | Max Gap | Position RMSE | Notes |
|--------------|---------|---------------|-------|
| 100% | N/A | 1.17m | Full GPS baseline |
| 10% | ~100s | 4.61m | Highway driving |
| 5% | ~120s | 5.75m | Extreme stress test |

## Quick Start

**Run (live tracking):**
```bash
cd ~/gojo
./motion_tracker_rs.sh 10  # 10-minute run (gyro on by default)
# Logs: motion_tracker_sessions/comparison_*.json.gz
```

**Replay (offline analysis):**
```bash
cd ~/gojo/motion_tracker_rs
cargo run --bin replay -- --log ../motion_tracker_sessions/comparison_YYYYMMDD_HHMMSS.json.gz

# Test GPS decimation (simulated denial):
cargo run --bin replay -- --log <logfile> --gps-decimation 10  # 10% GPS coverage
cargo run --bin replay -- --log <logfile> --gps-decimation 20  # 5% GPS coverage

# Optional: --enable-mag --enable-baro to mirror runtime fusion
```

## Termux:Widget Integration

Create these in `~/.shortcuts/` for one-tap control:

**~/.shortcuts/Start_Tracking**
```bash
#!/data/data/com.termux/files/usr/bin/bash
export HOME="/data/data/com.termux/files/home"
export PATH="/data/data/com.termux/files/usr/bin:$PATH"
exec >"$HOME/mt_widget.log" 2>&1

pkill -f "motion_tracker_rs/target/release/motion_tracker" 2>/dev/null
cd "$HOME/gojo/motion_tracker_rs" || exit 1

nohup /data/data/com.termux/files/usr/bin/cargo run --release --bin motion_tracker -- 7200 >/dev/null 2>&1 &
termux-toast "Tracking started"
```

**~/.shortcuts/Stop_Tracking**
```bash
#!/data/data/com.termux/files/usr/bin/bash
pkill -f "target/release/motion_tracker"
termux-toast "Tracking stopped"
```

## Log Format

- Stored in `motion_tracker_sessions/` (git-ignored)
- Each log includes:
  - Raw sensor data (accel, gyro, GPS)
  - 15D EKF state trajectory (position, velocity, attitude, biases)
  - Incidents (ZUPT events, GPS gaps, speed clamps)
  - Metrics (RMSE, max speeds, distances)
  - Optional mag/baro samples

## Current Status

### Validated ✅
- 15D EKF with cross-covariance GPS updates
- NHC with auto-calibrated mounting offset
- Heading-aided gyro bias estimation
- GPS decimation testing framework
- Dead reckoning through 2-minute gaps
- ~1m position accuracy with full GPS
- <6m accuracy with sparse GPS (10% coverage)

### In Development 🚧
- **Map Matching**: R-Tree based rail-snapping using tiled OSM data (Zoom 12 tiles, 3x3 buffer window)

## Architecture

- **Rust 15D EKF** lives in `motion_tracker_rs/`. Build with `cargo build`.
- **Phone-friendly**: Designed to run on modest Termux setups without external deps.
- **Clean codebase**: 13D shadow filter removed; pure 15D implementation.

## Tuning Parameters

Current conservative defaults optimized for stability:

| Parameter | Current | Effect |
|-----------|---------|--------|
| `q_pos` | `0.25 * dt⁴ * accel_var` | Position process noise |
| `q_vel` | 2.0 | Velocity recovery rate |
| GPS noise floor | 5m | Minimum GPS uncertainty |
| Innovation gate | 100m | Reject GPS jumps > 100m |
| NHC noise | `R[1,1]=R[2,2]=0.01` | Lateral/vertical constraint strength |

## Future Directions

1. **Map Matching**: Constrain lateral drift using road geometry via R-Tree lookups
2. **Visual-Inertial Odometry**: ARCore integration for indoor/GPS-denied navigation

---

Hack on it, replay your drives, and tweak fusion flags to suit your sensor suite.
