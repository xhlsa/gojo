# Replay NIS Tuning - Quick Start Guide

## Test Q Without Real Drives

Use `replay` to test different Q values on your golden dataset **without** draining phone battery.

## Single File Test

```bash
cd ~/gojo/motion_tracker_rs

# Test with default Q (0.5)
./target/release/replay --log ../motion_tracker_sessions/golden/comparison_20251126_183814.json.gz

# Output shows NIS immediately:
# ❌ comparison_20251126_183814.json.gz | NIS: 39.74 (OVERCONFIDENT) | RMSE: 1.17m | Q_vel: 0.50
```

**Interpretation**:
- NIS: 39.74 → **Way too high** (target: 2.0-4.0)
- Verdict: OVERCONFIDENT → Filter trusts IMU too much
- Action: Increase Q_vel

### Test Different Q Values

```bash
# Try Q = 1.0 (2x increase)
./target/release/replay --log ../golden/comparison_20251126_183814.json.gz --q-vel 1.0

# Try Q = 2.0 (4x increase)
./target/release/replay --log ../golden/comparison_20251126_183814.json.gz --q-vel 2.0

# Try Q = 3.0
./target/release/replay --log ../golden/comparison_20251126_183814.json.gz --q-vel 3.0
```

**Goal**: Find Q where NIS avg is closest to 3.0

## Batch Test All Golden Files

```bash
./target/release/replay --golden-dir ../motion_tracker_sessions/golden/ --q-vel 2.0
```

**Output**:
```
=== NIS (Normalized Innovation Squared) Summary ===
❌ comparison_20251125_182714.json.gz | NIS: 42.11 (OVERCONFIDENT) | RMSE: 1.45m | Q_vel: 2.00
✅ comparison_20251126_183814.json.gz | NIS: 3.14 (GOOD) | RMSE: 1.17m | Q_vel: 2.00
⚠️  comparison_20251126_191203.json.gz | NIS: 5.82 (MARGINAL) | RMSE: 2.03m | Q_vel: 2.00

Target NIS: 2.0-4.0 (ideal: ~3.0)
Tuning: Increase Q if OVERCONFIDENT, Decrease Q if UNDERCONFIDENT
```

**Analysis**:
- 1/3 files show GOOD → Q=2.0 is better than Q=0.5
- 1/3 still OVERCONFIDENT → May need even higher Q (or that file has issue)
- Average across all files should guide final Q choice

## Rapid Q Sweep

Test multiple Q values quickly:

```bash
for q in 0.5 1.0 1.5 2.0 2.5 3.0 3.5; do
  echo "=== Testing Q = $q ==="
  ./target/release/replay --log ../golden/comparison_20251126_183814.json.gz --q-vel $q \
    2>&1 | grep "NIS:"
done
```

**Output**:
```
=== Testing Q = 0.5 ===
❌ ... | NIS: 39.74 (OVERCONFIDENT) ...
=== Testing Q = 1.0 ===
❌ ... | NIS: 15.23 (OVERCONFIDENT) ...
=== Testing Q = 2.0 ===
✅ ... | NIS: 3.14 (GOOD) ...
=== Testing Q = 3.0 ===
⚠️  ... | NIS: 1.82 (MARGINAL) ...
```

**Winner**: Q = 2.0 (closest to 3.0)

## Apply Tuned Q to Code

Once you find optimal Q, update `src/filters/ekf_15d.rs`:

```rust
// Line ~100: Velocity process noise
let q_vel = 2.0;  // Updated from 0.5 to 2.0 based on NIS tuning
```

Rebuild:
```bash
cargo build --release
```

## Advanced: GPS Decimation Testing

Test Q under GPS-denied conditions:

```bash
# Simulate 10x GPS decimation (90% denial)
./target/release/replay \
  --log ../golden/comparison_20251126_183814.json.gz \
  --q-vel 2.0 \
  --gps-decimation 10
```

**Use Case**: Validate that Q tuning doesn't make filter unstable during long GPS gaps (tunnels, parking garages).

## Understanding NIS Metrics

From JSON output:

```json
{
  "nis_avg": 3.14,        // Mean NIS across all GPS updates
  "nis_median": 2.98,     // Median (less affected by outliers)
  "nis_min": 0.52,        // Minimum NIS (best fit)
  "nis_max": 185.67,      // Maximum NIS (outlier)
  "nis_count": 1139,      // Number of GPS updates
  "nis_verdict": "GOOD"   // Automatic verdict
}
```

**Key Insight**: If `nis_median` is good but `nis_avg` is high, you have outliers (e.g., GPS jumps at start/end of drive). This is normal.

## Troubleshooting

### Q Change Doesn't Affect NIS

**Problem**: Changed `--q-vel` but NIS stays the same

**Cause**: The flag only affects velocity process noise. NIS is dominated by position innovation.

**Fix**: For comprehensive Q tuning, edit `src/filters/ekf_15d.rs` directly:
```rust
// Line ~93: Position process noise
let q_pos = 0.25 * dt.powi(4) * accel_var;  // Modify this multiplier

// Line ~100: Velocity process noise
let q_vel = 2.0;  // Modify this value
```

### NIS Always OVERCONFIDENT

**Possible Causes**:
1. GPS accuracy values too optimistic (check `accuracy` field in logs)
2. IMU biases not converging (check `accel_bias`, `gyro_bias` fields)
3. Cold start protocol skipping first GPS fix (expected)

### NIS Varies Wildly Between Files

**Normal**: Different drives have different GPS quality:
- Highway: Low NIS (good GPS)
- Urban: High NIS (multipath)

**Action**: Tune Q for the **median** NIS across all files, not the worst case.

---

**Last Updated**: December 3, 2025
**Status**: NIS replay testing ready, testbed active
