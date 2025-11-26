# Gojo Motion Tracker (Termux + Rust)

A lean, on-device sensor-fusion logger for Android/Termux. It ingests your phone’s accelerometer, gyroscope, GPS (and optional magnetometer/barometer), runs a 15D EKF in Rust, and spits out comparison logs you can replay offline. Built to be hackable, measurable, and friendly to low-power phones.

## Why Try It?
- **Termux-first**: No Play Store deps; uses `termux-sensor` + `termux-location` directly.
- **Rust core**: 15D EKF with optional mag/baro fusion; replayable for A/B tuning.
- **Transparent data**: Every run saves a `comparison_*.json.gz` with raw sensors, EKF states, incidents, and metrics.
- **Road-tested**: Current tuning yields ~1 m/s RMSE on 30 m/s highway runs; gap handling with clamped speed and NHC keeps things stable during GPS outages.

## Sensors & Fusion
- **Required**: Accelerometer, Gyroscope, GPS.
- **Optional (off by default)**: Magnetometer yaw assist (`--enable-mag`), Barometer vertical damping (`--enable-baro`).
- **Gating**: Mag only when speed > 5 m/s and GPS gap > 3s; Baro only when last GPS speed > 3 m/s.

## Quick Start (Termux)
```bash
cd ~/gojo
./motion_tracker_rs.sh 10            # 10-minute run (gyro on by default)
# Logs: motion_tracker_sessions/comparison_*.json.gz
```

Replay (offline):
```bash
cd ~/gojo/motion_tracker_rs
cargo run --bin replay -- --log ../motion_tracker_sessions/comparison_YYYYMMDD_HHMMSS.json.gz
# Optional: --enable-mag --enable-baro to mirror runtime fusion
```

## Scripts
- `motion_tracker_rs.sh` — launch the Rust tracker via Termux sensors/GPS.
- `test_ekf.sh` — legacy harness; use `motion_tracker_rs.sh` for current runs.
- `replay` (Rust bin) — offline EKF replay with RMSE/clamp stats.

## Output
- Stored in `motion_tracker_sessions/` (git-ignored).
- Each log includes raw sensors, EKF states, incidents, metrics, and optional mag/baro samples.

## Current Status
- **Accuracy**: ~1 m/s RMSE on recent highway drives; stable max speed tracking with GPS clamp and NHC.
- **Stability**: Handles 15s GPS gaps with speed clamps; mag/baro kept opt-in to avoid surprises.
- **Defaults**: Conservative and stable; fusion extras are opt-in.

## Build Notes
- Rust 15D EKF lives in `motion_tracker_rs/`. Build with `cargo build`.
- Phone-friendly: designed to run on modest Termux setups without external deps.

Hack on it, replay your drives, and tweak fusion flags to suit your sensor suite.
