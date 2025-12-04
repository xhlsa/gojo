# NIS (Normalized Innovation Squared) Tuning Guide

## What is NIS?

NIS is a statistical consistency check that tells you if your Kalman Filter is "sane". It measures whether your filter's uncertainty (covariance P) matches reality.

**Formula**: `NIS = innovation^T * S^-1 * innovation`

Where:
- `innovation` = measurement - prediction (how far off were we?)
- `S` = innovation covariance (H*P*H^T + R)

## The 95% Rule

For a **3D position update** (3 degrees of freedom), your NIS should average around **3.0**.

```
NIS Statistics Table (Chi-Squared Distribution, 3 DOF):

NIS Range     | Verdict            | Action
--------------|--------------------|----------------------------------
< 0.5         | Underconfident     | Decrease Process Noise (Q)
0.5 - 2.0     | Slightly Low       | Monitor, maybe decrease Q by 10%
2.0 - 4.0     | ✅ GOOD           | Filter is well-tuned
4.0 - 10.0    | Slightly High      | Monitor, maybe increase Q by 10%
> 10.0        | Overconfident      | Increase Process Noise (Q)
```

## Live Monitoring

Your status output now shows NIS with real-time tuning hints:

```
[STATUS] Accel: 1234, Gyro: 1234, GPS: ✅ LOCKED | Accept: 95.3% | NIS: 3.14 ✅ GOOD, Mem: 45.2MB
```

**Status Indicators**:
- `✅ GOOD` - NIS between 2.0-4.0, filter is well-tuned
- `⚠️  MARGINAL` - NIS slightly outside optimal range
- `❌ OVERCONFIDENT (increase Q)` - NIS > 10.0, filter ignoring GPS
- `⚠️  UNDERCONFIDENT (decrease Q)` - NIS < 0.5, filter too jumpy

## What Q (Process Noise) Does

**Process Noise (Q)** represents how much you trust the IMU prediction vs GPS measurements.

### Symptoms of Q Too Low (Overconfident)
```
NIS: 15.2 ❌ OVERCONFIDENT (increase Q)
GPS: Accept: 45.3% (rejecting valid GPS updates)
```

**Problem**: Filter thinks its IMU integration is perfect, ignores GPS corrections
**Fix**: Increase Q for position/velocity by 50-100%

**File**: `motion_tracker_rs/src/filters/ekf_15d.rs`
```rust
// Line ~93: Position process noise
let q_pos = 0.25 * dt.powi(4) * accel_var;  // INCREASE this multiplier

// Line ~100: Velocity process noise
let q_vel = 2.0;  // INCREASE this value (try 3.0 or 4.0)
```

### Symptoms of Q Too High (Underconfident)
```
NIS: 0.3 ⚠️  UNDERCONFIDENT (decrease Q)
Position: Jumpy, snaps to GPS every update
```

**Problem**: Filter doesn't trust IMU, follows GPS blindly (defeats purpose of fusion)
**Fix**: Decrease Q for position/velocity by 30-50%

## Tuning Workflow

### 1. Collect Baseline Data
```bash
cd ~/gojo
./motion_tracker_rs.sh 5  # 5-minute drive
```

Watch console output:
```
[NIS] Current: 8.24, Avg: 7.12, Range: [2.14, 15.32] (target: 3.0)
[STATUS] ... | NIS: 7.12 ❌ OVERCONFIDENT (increase Q)
```

### 2. Interpret Results

**NIS Average: 7.12** → Overconfident
**Action**: Increase Q

**NIS Range: [2.14, 15.32]** → High variance
**Cause**: GPS accuracy varies (urban/highway), this is normal

### 3. Adjust Q Parameters

Edit `src/filters/ekf_15d.rs`:

```rust
// BEFORE (NIS ~7.0, overconfident):
let q_vel = 2.0;

// AFTER (try this):
let q_vel = 3.5;  // 75% increase
```

Rebuild and test:
```bash
cargo build --release
./motion_tracker_rs.sh 5
```

### 4. Validate

Target metrics after tuning:
- **NIS Average**: 2.5 - 3.5
- **GPS Accept Rate**: > 85%
- **Consecutive Rejections**: < 3

## Advanced: Per-Axis Q Tuning

If NIS is still off after global Q adjustment, you can tune individual axes:

```rust
// In ekf_15d.rs, modify process_noise initialization
for i in 0..3 {
    process_noise[[i, i]] = q_pos;  // Position (X, Y, Z)
}

// If Z-axis is problematic, adjust separately:
process_noise[[2, 2]] = q_pos * 2.0;  // Z-axis gets more process noise
```

## Common Scenarios

### Scenario 1: Highway Driving (Smooth, Accurate GPS)
**Expected NIS**: 2.0 - 3.0
**Q Setting**: Standard (q_vel = 2.0)

### Scenario 2: Urban Canyon (Poor GPS)
**Expected NIS**: 5.0 - 8.0 (higher is OK due to bad GPS)
**Q Setting**: Slightly higher (q_vel = 2.5)
**Note**: High NIS here is GPS fault, not filter fault

### Scenario 3: Parking Garage (GPS Denied)
**Expected NIS**: N/A (no GPS updates)
**Mode**: Dead reckoning (rely on gravity well)

## Validation Tests

### Test 1: Stationary NIS
Park for 30 seconds, check NIS.

**Good**: NIS < 5.0 (GPS accuracy variance)
**Bad**: NIS > 15.0 (filter drifting while stationary)

### Test 2: Constant Velocity NIS
Highway cruise at 65 mph for 2 minutes.

**Good**: NIS = 2.0-4.0, GPS accept rate > 95%
**Bad**: NIS > 8.0, frequent rejections

### Test 3: Sharp Turn NIS
Make a 90° turn at 20 mph.

**Good**: NIS briefly spikes to 5-7, then recovers
**Bad**: NIS > 15, filter rejects GPS during turn

## Troubleshooting

### Problem: NIS Spikes Above 20 During Turns
**Cause**: Gyro bias incorrect or mounting offset wrong
**Fix**: Check mounting calibration, validate gyro bias convergence

### Problem: NIS = 0.1 (Too Low)
**Cause**: Q too high OR GPS accuracy input too optimistic
**Fix**: Verify GPS accuracy values, check if hardcoded 5m floor is too aggressive

### Problem: NIS Gradually Increases Over Time
**Cause**: IMU bias drift not being estimated correctly
**Fix**: Check accel/gyro bias updates (ZUPT, heading-aided bias)

## References

- Chi-Squared Distribution Table: https://en.wikipedia.org/wiki/Chi-squared_distribution
- Kalman Filter Consistency: Bar-Shalom, Y. "Estimation with Applications to Tracking and Navigation" (2001)
- Optimal Q Tuning: "A Practical Approach to Kalman Filter Tuning" (Gibbs, 2011)

---

**Last Updated**: December 3, 2025
**Status**: NIS monitoring active, ready for real-world tuning
