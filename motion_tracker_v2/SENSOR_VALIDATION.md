# Accelerometer Sensor Validation & Health Monitoring Guide

## Overview

Motion Tracker V2 now includes comprehensive sensor health monitoring and diagnostics. This guide explains what's being validated, when, and how to interpret the results.

---

## What Changed

### 1. **Sensor Initialization Validation**
**When**: Immediately after sensor daemon starts
**What's checked**:
- Is the daemon actually reading data?
- How many samples collected in 5 seconds?
- Is the sample rate correct (50 Hz ±20%)?
- Is timing stable (minimal jitter)?
- Do values make physical sense?

**Output**:
```
================================================================================
ACCELEROMETER STARTUP VALIDATION
================================================================================
Testing daemon for 5s, expecting ~250 samples at 50Hz...
.......................................
✓ Collected 245 samples in 5s
  Actual rate: 49.0 Hz (target: 50 Hz)
  Sample interval: 20.4ms ±1.2ms
  Magnitude range: 9.21 to 10.43 m/s²
  Mean magnitude: 9.81 m/s²
✓ STARTUP VALIDATION PASSED
```

**Common failures**:
- ✗ "NO SAMPLES COLLECTED" → Daemon isn't reading from sensor
- ✗ "Sample rate 10.0Hz" → termux-sensor not configured correctly
- ⚠ "High timing jitter" → System under heavy load, may skip samples

---

### 2. **Calibration Validation**
**When**: After initial calibration (keep device still)
**What's checked**:
- Is gravity magnitude reasonable (9.5-10.1 m/s²)?
- Are per-axis biases within physical limits (±15 m/s²)?
- Does calibration data look valid?

**Output**:
```
================================================================================
CALIBRATION VALIDATION
================================================================================
Calibration biases:
  X: -0.123 m/s²
  Y: +0.045 m/s²
  Z: +9.817 m/s²
  Gravity magnitude: 9.819 m/s²
✓ Gravity magnitude is valid
✓ CALIBRATION VALIDATION PASSED
```

**Common failures**:
- ✗ "Gravity magnitude too low (2.5m/s²)" → Phone was moving during calibration
- ✗ "Bias Z is very large (45.2m/s²)" → Sensor hardware issue

---

### 3. **Runtime Data Quality Monitoring**
**When**: Continuously during tracking
**What's checked**:
- Are values NaN/Inf/unreasonable (>100 m/s²)?
- Has gravity magnitude changed (recalibration needed)?
- Is queue stalling (no data for >2 seconds)?
- Is sample rate still healthy?

**Output** (every 30 seconds):
```
⚠ ACCEL HEALTH: Sample rate 45.2Hz out of range
```

Or silently passes if all good.

---

### 4. **Final Diagnostics Report**
**When**: Program exits
**What's shown**:
- Startup validation status
- Startup samples collected
- Final gravity magnitude
- Current sample rate
- Queue status
- Gravity drift detected (yes/no)
- Queue stall count (number of times it paused)
- All warnings and errors that occurred

**Output**:
```
================================================================================
ACCELEROMETER HEALTH DIAGNOSTICS
================================================================================

Initialization:
  Startup validated: ✓ Yes
  Startup samples: 245

Calibration:
  Gravity magnitude: 9.819

Runtime:
  Sample rate: 49.1 Hz (✓ Healthy)
  Queue status: ✓ Active
  Time since last sample: 0ms
  Gravity drift detected: ✓ No
  Queue stall count: 0

✓ No errors or warnings
================================================================================
```

---

## Key Changes to Code

### Acceleration Calculation (FIXED)
**Old code** (broken for tilted phones):
```python
horizontal_accel = math.sqrt(accel_data['x']**2 + accel_data['y']**2)
```

**Problem**: Assumes phone is perfectly level. If tilted 45°, can give 3x wrong acceleration.

**New code** (works at ANY tilt):
```python
motion_accel = accel_data.get('magnitude', 0)
```

**How it works**:
- `magnitude` = sqrt(x² + y² + z²) (total acceleration measured)
- gravity_magnitude ≈ 9.81 m/s² (always the same)
- motion = magnitude - gravity
- **Result**: Works at ANY phone orientation ✓

---

## How to Use

### Normal Operation
```bash
cd ~/gojo/motion_tracker_v2
python motion_tracker_v2.py
# or
python motion_tracker_v2.py 10  # 10 minute run
```

You'll see:
1. **Startup validation** - 5 second test of sensor
2. **Calibration validation** - 10 sample calibration check
3. **Running display** - Normal tracking output
4. **Periodic alerts** - If health issues detected
5. **Final diagnostics** - Complete report on exit

### Interpreting Alerts

#### ✓ Green (All Good)
```
✓ Collected 245 samples in 5s
✓ Gravity magnitude is valid
✓ STARTUP VALIDATION PASSED
```
→ Sensor is working perfectly

#### ⚠ Yellow (Warning)
```
⚠ WARNING: Sample rate 40.0Hz is outside expected range
⚠ Gravity drift detected, recalibration may be needed
```
→ Sensor working but with issues. Data quality may be slightly reduced.

#### ✗ Red (Error)
```
✗ FAILED: No accelerometer samples received
✗ ERROR: Gravity magnitude too low (2.5m/s²)
```
→ Sensor not working. Data is invalid.

---

## Troubleshooting

### "NO SAMPLES COLLECTED - Sensor daemon is not reading data"
**Cause**: `termux-sensor` can't access the accelerometer
**Solutions**:
1. Check if Termux has sensor permission: Settings → Permissions → Sensors
2. Test manually: `termux-sensor -s "lsm6dso LSM6DSO Accelerometer Non-wakeup"`
3. Try different sensor: `termux-sensor -l` to list all sensors

### "Sample rate 10.0Hz is outside expected range"
**Cause**: System under heavy load or sensor too slow
**Solutions**:
1. Close other apps
2. Reduce other background tasks
3. Check if `-d 20` (20ms delay = 50Hz) is being respected

### "Gravity magnitude 2.5m/s² is too low"
**Cause**: Phone was moving during calibration
**Solution**: Repeat run, keep phone perfectly still during 10-second calibration

### "Queue stalled for 3000ms"
**Cause**: Sensor daemon crashed or sensor frozen
**Solutions**:
1. Check daemon with: `termux-sensor -s "lsm6dso..."`
2. Restart if needed
3. Check if sensor is physically accessible (not blocked)

### "Gravity drift detected"
**Cause**: Normal. Phone rotated, gravity now in different axis
**Note**: Dynamic recalibration should handle this. If persistent, may need manual restart.

---

## What's Actually Being Tested

### 1. Daemon Startup (5 seconds)
```
Start sensor daemon → Wait 5 seconds → Count samples
Expected: ~250 samples (50 Hz)
Pass threshold: 200 samples (80% of expected)
```

### 2. Calibration (10 samples)
```
Collect 10 still readings → Average X, Y, Z
Calculate gravity = sqrt(X² + Y² + Z²)
Expected: 9.5 - 10.1 m/s² (Earth's gravity)
```

### 3. Runtime (every sample)
```
New sample arrives
- Check for NaN/Inf
- Calculate magnitude
- Compare to previous gravity
- Track timestamp
```

### 4. Health Check (every 30 seconds)
```
Calculate current sample rate from recent timestamps
Check if queue has fresh data
Alert if any critical issue detected
```

---

## Technical Details

### Gravity Magnitude
| Location | Expected Value |
|----------|---|
| Earth surface | 9.80-9.83 m/s² |
| Typical phone | 9.81 m/s² |
| Valid range | 9.5-10.1 m/s² |

Anything outside this range means:
- Phone was moving during calibration, OR
- Sensor is damaged

### Sample Rate
| Target | Min OK | Max OK |
|--------|--------|--------|
| 50 Hz | 40 Hz | 60 Hz |
| (20ms) | (±20%) | (±20%) |

### Queue Stall Timeout
- **Threshold**: 2 seconds without a sample
- **Alert**: Printed immediately
- **Interpretation**: Sensor daemon likely crashed or sensor is frozen

---

## File Structure

```
motion_tracker_v2/
├── motion_tracker_v2.py              ← Main app (MODIFIED)
├── accel_health_monitor.py           ← NEW: Health monitoring
├── accel_calculator.py               ← NEW: Proper acceleration extraction
├── accel_processor.pyx               ← Existing Cython (unchanged)
├── setup.py                          ← Build config (unchanged)
└── SENSOR_VALIDATION.md              ← This file
```

---

## Quick Reference

### What's Being Validated

| When | What | Pass Criteria |
|------|------|---|
| **Startup** | Can daemon read? | ≥200 samples in 5s |
| **Startup** | Sample rate OK? | 40-60 Hz |
| **Startup** | Values reasonable? | 8-12 m/s² |
| **Calibration** | Gravity valid? | 9.5-10.1 m/s² |
| **Calibration** | Biases OK? | -15 to +15 m/s² |
| **Runtime** | No NaN/Inf? | All values finite |
| **Runtime** | Rate still OK? | 40-60 Hz |
| **Runtime** | Queue active? | Fresh sample every 2s |

### Impact on Tracking Data

| Issue | Severity | Impact on Tracking |
|-------|----------|---|
| Startup failed | CRITICAL | No acceleration data collected |
| Gravity invalid | CRITICAL | Acceleration measurements wrong by 10x+ |
| Sample rate low | MEDIUM | Acceleration updates less frequent |
| Queue stalled | HIGH | Data gaps, may skip acceleration periods |
| Gravity drift | LOW | Small errors until recalibration |

---

## Example Session Output

```
================================================================================
GPS + ACCELEROMETER MOTION TRACKER V2 - Multithreaded Edition
================================================================================

Configuration:
  Duration: Continuous (Ctrl+C to stop)
  Accelerometer: 50 Hz
  Auto-save: Every 2 minutes

Starting in 3 seconds...

Starting background sensor threads...

================================================================================
ACCELEROMETER STARTUP VALIDATION
================================================================================
Testing daemon for 5s, expecting ~250 samples at 50Hz...
.................................................
✓ Collected 248 samples in 5s
  Actual rate: 49.6 Hz (target: 50 Hz)
  Sample interval: 20.2ms ±0.8ms
  Magnitude range: 9.19 to 10.32 m/s²
  Mean magnitude: 9.81 m/s²
✓ STARTUP VALIDATION PASSED

Calibrating accelerometer (keep device still)...
..........
✓ Calibrated. Bias: x=-0.08, y=+0.12, z=+9.81, Gravity: 9.819 m/s²

================================================================================
CALIBRATION VALIDATION
================================================================================
Calibration biases:
  X: -0.080 m/s²
  Y: +0.120 m/s²
  Z: +9.809 m/s²
  Gravity magnitude: 9.819 m/s²
✓ Gravity magnitude is valid
✓ CALIBRATION VALIDATION PASSED

✓ GPS thread started
✓ Accelerometer thread started (50 Hz)

Waiting for GPS fix...
✓ GPS locked: 37.774321, -122.416911

Tracking... (Press Ctrl+C to stop)

Auto-save enabled: every 2 minutes
Accelerometer sampling: 50 Hz
Time     | Speed (km/h) | Distance (m) | Accel      | GPS Acc
--------------------------------------90
0:00     |       0.00   |         0.0  |   0.00     |    5.2m
0:01     |       8.45   |        42.1  |   2.45     |    4.8m
0:02     |      35.20   |       273.5  |   1.05     |    3.5m
0:03     |      45.10   |       718.2  |   0.23     |    3.2m
...
5:00     |      25.34   |     12541.8  |   0.45     |    4.1m

================================================================================
FINAL STATE
================================================================================
Session duration:     5m 0s
Total distance:       12541.8 m (12.54 km)
GPS samples (in memory): 302
Accelerometer samples (in memory): 15024

Average speed:        50.2 km/h
Max speed:            65.5 km/h

Battery:
  Start: 85%
  End:   82%
  Drop:  3%

Auto-saves performed: 0
================================================================================

Final Accelerometer Diagnostics:

================================================================================
ACCELEROMETER HEALTH DIAGNOSTICS
================================================================================

Initialization:
  Startup validated: ✓ Yes
  Startup samples: 248

Calibration:
  Gravity magnitude: 9.819

Runtime:
  Sample rate: 50.1 Hz (✓ Healthy)
  Queue status: ✓ Active
  Time since last sample: 15ms
  Gravity drift detected: ✓ No
  Queue stall count: 0

✓ No errors or warnings
================================================================================

```

---

## Next Steps

After you run Motion Tracker V2 with this new validation:

1. **Check the startup validation** - Does it pass?
2. **Check the calibration validation** - Are biases reasonable?
3. **Look for runtime alerts** - Did any warnings appear?
4. **Review final diagnostics** - Any issues reported?

If everything passes ✓ → Your accelerometer is working correctly!

If there are warnings ⚠ → Review the troubleshooting section above.

If there are errors ✗ → Don't trust the acceleration data; data may be corrupted.
