# Gojo Motion Tracker (Termux)

Lightweight sensor-fusion logger for Android/Termux. Captures accelerometer, gyroscope, GPS (and optional magnetometer/barometer) into comparison JSON files for EKF vs complementary filter analysis.

## Quick Start

```bash
cd ~/gojo
./motion_tracker_rs.sh 10        # 10-minute run (default: gyro on)
# Output: motion_tracker_sessions/comparison_*.json.gz
```

Flags (runtime):
- `--enable-mag` to fuse magnetometer yaw assist (off by default)
- `--enable-baro` to fuse barometer vertical damping (off by default)

Replay (offline analysis):
```bash
cd ~/gojo/motion_tracker_rs
cargo run --bin replay -- --log ../motion_tracker_sessions/comparison_YYYYMMDD_HHMMSS.json.gz
```
Use `--enable-mag/--enable-baro` to mirror runtime fusion in replay.

## Scripts
- `motion_tracker_rs.sh` — run the Rust tracker via Termux sensors/GPS.
- `test_ekf.sh` — legacy harness; use `motion_tracker_rs.sh` for current flow.
- `replay` (Rust bin) — offline EKF replay and metrics.

## Output
- Stored under `motion_tracker_sessions/` (ignored in git).
- Each `comparison_*.json.gz` includes raw sensors, EKF states, incidents, and metrics.

## Dev Notes
- Rust EKF (15D) with optional mag/baro fusion (opt-in).
- Baro gated by last GPS speed > 3 m/s; mag gated by speed > 5 m/s and GPS gap > 3s.
- Use `cargo build` in `motion_tracker_rs/`; defaults tuned for in-car testing.

## Status
Active development; defaults are conservative and stable. Internal docs are ignored from git for public release.
