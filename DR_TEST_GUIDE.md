# Pure Dead Reckoning Test Mode

## What It Does

Added `--gps-init-only` flag to replay.rs that:
- Accepts only the **first GPS fix** for position/velocity initialization
- Ignores **all subsequent GPS updates** for EKF corrections
- Continues IMU integration (gyro + accel) for the entire drive
- Still logs ground-truth GPS for analysis (in JSON output)

## Running the Test

```bash
cd ~/gojo/motion_tracker_rs

# Build
cargo build --release --bin replay

# Test on golden drive
../target/release/replay --log ../motion_tracker_sessions/golden/comparison_20251126_183814.json.gz --gps-init-only
```

Output:
```
[GPS-INIT-ONLY] First GPS fix applied at t=1764179917.0s, lat=32.215737, lon=-110.841166
[GPS-INIT-ONLY] Entering pure dead reckoning mode - all subsequent GPS fixes ignored
{...stats...}
```

## Why This Is Useful

Tests **pure IMU integration quality** under realistic conditions:

| Error Source | Impact After 40 min |
|---|---|
| Gyro bias (~0.5°/s) | 20° heading drift/minute |
| Accel bias + noise | 10-50m position/minute |
| **Total accumulation** | Potentially **kilometers off** |

## Expected Failure Modes

1. **Heading divergence**: Yaw accumulates → trajectory curves wrong direction
2. **Speed estimate drift**: Accel bias → overshoot distance
3. **Lateral drift**: Gyro creep → perpendicular error growth
4. **Your NHC helps**: Even without GPS, lateral constraint fires (using EKF speed)

## Interpreting Results

The trajectory from `comparison_*.json.gz` already contains the full GPS-fused result.
To truly test dead reckoning, you'd need to:

1. **Capture new data** with live motion tracker
2. **Process same drive twice**:
   ```bash
   # Full GPS (normal mode)
   cargo run --release --bin replay -- --log drive.json.gz > full_gps.json

   # Pure DR (init-only mode)
   cargo run --release --bin replay -- --log drive.json.gz --gps-init-only > pure_dr.json
   ```
3. **Compare trajectory divergence** as function of time

## Code Locations

- **Flag definition**: `src/bin/replay.rs:74-76`
- **GPS skip logic**: `src/bin/replay.rs:508` (gps_init_only_skip)
- **Logging**: `src/bin/replay.rs:542-546`

