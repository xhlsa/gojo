# Termux Sensor Integration Guide

## Overview
This document captures research, lessons learned, and practical patterns for accessing sensors in Termux via Python.

---

## 🔍 Research Findings

### Why `termux-sensor`?
**Tested Approaches:**

| Approach | Verdict | Notes |
|----------|--------|-------|
| **termux-sensor (CLI)** | ✓ Best for Termux | Universal, JSON output, no device-specific workarounds |
| **pyjnius (Android SensorManager)** | ⚠️ Complex | Direct API access but setup is finicky, not guaranteed on all Termux builds |
| **/sys/class/sensors/** | ❌ Unreliable | Device-specific, many modern phones don't expose sensors here, no standardization |

**Lesson**: Use `termux-sensor` subprocess. It's the Termux-native approach, guaranteed to work, outputs clean JSON.

---

## 📋 termux-sensor Command Reference

### Basic Usage
```bash
# List all available sensors
termux-sensor -l

# Read accelerometer only (10 samples, 200ms interval)
termux-sensor -s accel -n 10 -d 200

# Read multiple sensors
termux-sensor -s "Accel" "Gyro"
```

### Options
| Flag | Purpose | Example |
|------|---------|---------|
| `-l, list` | Show all available sensors | `termux-sensor -l` |
| `-s, sensors` | Specify sensor(s) to read (partial match OK) | `termux-sensor -s accel` |
| `-n, limit` | Number of samples before exit | `termux-sensor -n 50` |
| `-d, delay` | Milliseconds between samples | `termux-sensor -d 100` |
| `-a, all` | Monitor all sensors (battery drain warning!) | `termux-sensor -a` |

### Key Insight: Sensor Name Matching
- **Exact names not required** - "accel" matches "lsm6dso LSM6DSO Accelerometer Non-wakeup"
- **Case insensitive** - "ACCEL" = "accel"
- **Device-dependent** - Your device may have LSM6DSO, BMI160, or other accelerometer chips

**On test device (Samsung S24):**
```
lsm6dso LSM6DSO Accelerometer Non-wakeup  (main accel sensor)
linear_acceleration                        (processed accel: gravity removed)
lsm6dso LSM6DSO Accelerometer-Uncalibrated (raw, no bias correction)
```

---

## 📊 Output Format & Parsing

### Raw termux-sensor Output
The command outputs **multiline pretty-printed JSON**, one complete object per sensor reading:

```json
{
  "lsm6dso LSM6DSO Accelerometer Non-wakeup": {
    "values": [
      0.15493527054786682,
      9.54197883605957,
      -2.544766902923584
    ]
  }
}
```

### Critical Parsing Lesson
❌ **Don't assume one line = one JSON object**
```python
# WRONG - will fail on multiline JSON
for line in output.split('\n'):
    data = json.loads(line)  # Crashes on "{"
```

✓ **Use brace counting to reconstruct objects**
```python
# CORRECT - buffer lines until braces match
buffer = ""
brace_count = 0

for line in output:
    buffer += line
    brace_count += line.count('{') - line.count('}')

    if brace_count == 0 and buffer.strip():
        data = json.loads(buffer)  # Complete object
        buffer = ""
```

**See:** `accel_reader.py:105-128` for production implementation.

### Data Structure
- **Sensor name is the top-level key** (varies by device)
- **Always has `"values"` array with 3+ floats** (X, Y, Z, etc.)
- **No timestamp** in termux-sensor output (use `time.time()` if needed)

---

## 🎯 Device-Specific Considerations

### Available Sensors
Your device exposes **46 different sensors**, but most are computed from a few base sensors:

**Base sensors (hardware):**
- `lsm6dso LSM6DSO Accelerometer` - Raw accelerometer (6-axis IMU chip)
- `lsm6dso LSM6DSO Gyroscope` - Raw gyroscope
- `AK09918 Magnetometer` - Compass
- `lps22hh Pressure Sensor` - Altitude
- `TMD4913 Light` - Ambient light

**Derived sensors (firmware processing):**
- `linear_acceleration` - Accel minus gravity (useful for motion detection)
- `gravity` - Just the gravity component
- `step_detector` - Footstep counter
- `Rotation Vector` - Quaternion orientation

**Lesson**: For motion tracking, use `lsm6dso LSM6DSO Accelerometer` (raw) or `linear_acceleration` (gravity-removed). Device APIs handle different chips automatically via sensor abstraction.

### Sampling Rate Limits
- **Hardware typically**: 50-500 Hz capable
- **termux-sensor `-d` flag**: Limits polling interval (e.g., `-d 50` = 20 Hz max)
- **Practical limit**: ~50 Hz without battery drain concern
- **Higher rates**: Possible but not tested; may cause API throttling

---

## ⚡ Performance & Reliability Patterns

### Pattern 1: Use `Popen` for Long-Running Reads
```python
proc = subprocess.Popen(
    "termux-sensor -s accel -n 1000 -d 50",
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True
)

for line in proc.stdout:
    # Process real-time, don't buffer
```

**Why**: `check_output()` buffers entire output → memory overhead for long sessions.

### Pattern 2: Graceful Shutdown
```python
try:
    proc.wait(timeout=60)
except subprocess.TimeoutExpired:
    proc.kill()  # Force cleanup
```

**Why**: Prevent zombie processes on error.

### Pattern 3: Error Resilience
```python
try:
    data = json.loads(buffer)
except json.JSONDecodeError:
    # Skip malformed line, continue reading
    buffer = ""
    continue
```

**Why**: Occasional sensor glitches or transient output can break parsing; graceful degradation is better than crash.

---

## 🔧 Building Reliable Sensor Apps

### Lifecycle Pattern (Recommended)
```python
class SensorReader:
    def awaken(self):
        """Initialize: verify sensor exists"""
        # Check termux-sensor -l for sensor availability

    def read(self, num_samples, interval_ms):
        """Read: collect data from active sensor"""
        # Use Popen + brace counting

    def shutdown(self):
        """Cleanup: reset state, free resources"""
        # Kill any lingering processes
```

**See:** `accel_reader.py` for full implementation.

### Testing Your Device
```bash
# Step 1: What sensors do you have?
termux-sensor -l | grep -i accel

# Step 2: Raw data quality check
termux-sensor -s "accel" -n 5 -d 200

# Step 3: Sampling reliability
termux-sensor -s "accel" -n 100 -d 50
# If you get all 100 samples cleanly, 50ms interval is stable
```

---

## ❌ Common Pitfalls & Solutions

| Problem | Cause | Solution |
|---------|-------|----------|
| `termux-sensor: command not found` | Not installed | `apt install termux-api` |
| Parsing crashes on `"{"` | Assuming line = object | Use brace counting |
| Only getting 1-2 samples | Wrong sensor name | Use partial match: `termux-sensor -s accel -l` |
| High CPU/battery drain | Reading all sensors | Use `-s` to target one sensor |
| Stale accelerometer data | Device orientation change | Implement dynamic recalibration (see Motion Tracker V2 notes) |
| JSON parse timeout | Sensor glitch | Add timeout + skip malformed lines gracefully |

---

## 📈 Integration with Motion Tracking

### GPS + Accelerometer Fusion
The accelerometer provides high-frequency motion detail (50 Hz) while GPS provides absolute position truth (1 Hz).

**Pattern:**
```python
reader = AccelerometerReader()
reader.awaken()

# In main loop:
samples = reader.read(num_samples=50, interval_ms=20)  # 50 Hz for 1 second
for sample in samples:
    velocity = estimate_from_accel(sample.x, sample.y, sample.z)
    fused_velocity = 0.7 * gps_velocity + 0.3 * accel_velocity
```

**Key lesson**: Accel needs calibration (gravity removal) to be useful. See Motion Tracker V2 for calibration patterns.

---

## 🎓 Lessons Learned

1. **Termux is not a full Android dev environment** - Use `termux-sensor` (Android API wrapper) instead of trying to access SensorManager directly.

2. **Sensor names are device-specific** - Always query with `-l` first, use partial matching in code.

3. **JSON parsing isn't trivial with pretty-printed output** - Brace counting beats regex for robustness.

4. **Raw accelerometer includes gravity (~9.81 m/s²)** - Subtract or use `linear_acceleration` sensor for motion-only data.

5. **Reliability requires timeout handling** - Sensors can glitch; design for graceful degradation.

6. **subprocess.Popen > check_output for long runs** - Memory efficiency matters over extended sessions.

7. **Sampling rate is tunable but has limits** - Start with 50 Hz (20ms interval), test your device's stability.

---

## 📚 References

- **Termux Wiki**: https://wiki.termux.com/wiki/Termux-sensor
- **termux-api**: Built-in tool to access Android APIs from shell
- **Production code**: See `accel_reader.py` for working implementation

---

## Next Steps

- [ ] Test with real motion data (car drive, walking)
- [ ] Implement gravity calibration for any orientation
- [ ] Fuse with GPS for complete motion tracking
- [ ] Consider Kalman filter for smoother fusion (future optimization)
