# Sensor Polling Rate Investigation: termux-sensor vs adb dumpsys

## Executive Summary

**Hardware Capability: ~17.3 Hz accelerometer sampling**

The device accelerometer hardware updates at approximately **17.3 Hz** consistently. Our current motion_tracker_v2 implementation using a persistent daemon achieves only ~1 Hz, leaving ~17x performance on the table.

The investigation revealed:
- `adb shell dumpsys sensorservice` is not directly available in Termux (requires adb connection)
- termux-sensor is already optimal for this device - it wraps the Termux API which interfaces with Android's sensor framework
- Tuning the `-d` (delay) parameter in termux-sensor affects API call overhead but NOT actual hardware rate
- **Best approach**: Use termux-sensor with appropriate delay setting for the desired overhead/accuracy tradeoff

---

## Detailed Findings

### 1. Hardware Acceleration Rate

The accelerometer on this device samples at a **fixed hardware rate of ~17.3 Hz**.

**Test Results (3-second collections):**

| Delay Setting | API Call Rate | Unique Hardware Updates | Actual Rate | Duplication |
|---|---|---|---|---|
| 1ms | 844.0 Hz | 52 | 17.3 Hz | 48.7x |
| 5ms | 213.7 Hz | 52 | 17.3 Hz | 12.3x |
| 10ms | 110.7 Hz | 52 | 17.3 Hz | 6.4x |
| 20ms | 56.3 Hz | 52 | 17.3 Hz | 3.2x |
| **50ms** | **22.7 Hz** | **51** | **17.0 Hz** | **1.3x** |

**Key insight:** The hardware cannot be made faster through software. Faster API calls just return duplicate cached values.

---

### 2. Why Current Persistent Daemon Gets ~1 Hz

Our current implementation in motion_tracker_v2:

```python
class PersistentAccelDaemon:
    def _read_loop(self):
        # Reads from: stdbuf -oL termux-sensor -s ACCELEROMETER
```

**Why it only achieves ~1 Hz:**

1. **Default delay behavior**: When no `-d` parameter is passed, termux-sensor seems to use a conservative default (likely 100ms or more)
2. **Process startup latency**: The daemon takes time to initialize before producing output
3. **JSON parsing overhead**: Multi-line JSON parsing and queuing adds latency

**Evidence**: Test 2 in test_sensor_rates.sh showed sequential API calls took ~1450ms each, suggesting high latency in the interaction model.

---

### 3. Why termux-sensor is Better Than Expected

termux-sensor is **not** a simple shell wrapper - it's an efficient interface to Android's sensor framework through Termux API.

**Architecture:**
```
termux-sensor (bash wrapper)
    ↓
termux-api Sensor (compiled binary interface)
    ↓
Android SensorManager HAL
    ↓
Hardware accelerometer (17.3 Hz)
```

**Benefits:**
- Direct HAL access via compiled binary (termux-api is optimized)
- Can tune polling frequency via `-d` parameter
- Returns JSON formatted data with sensor name and values

---

### 4. API Call Overhead

The actual overhead depends on how aggressively you poll:

**At optimal 50ms delay:**
- API call rate: 22.7 Hz (very close to hardware 17.3 Hz)
- Duplication factor: Only 1.3x
- Minimal wasted polling

**At aggressive 1ms delay:**
- API call rate: 844 Hz (48.7x overhead!)
- Duplication: 48.7x (most calls return same data)
- High CPU usage with zero benefit

---

## Optimization Recommendations

### Recommendation 1: Update PersistentAccelDaemon to use delay parameter

**Current:**
```python
self.sensor_process = subprocess.Popen(
    ['stdbuf', '-oL', 'termux-sensor', '-s', 'ACCELEROMETER'],  # No -d!
    ...
)
```

**Optimized:**
```python
self.sensor_process = subprocess.Popen(
    ['stdbuf', '-oL', 'termux-sensor', '-s', 'ACCELEROMETER', '-d', '50'],  # 50ms delay
    ...
)
```

**Expected improvement:** From ~1 Hz → ~17 Hz (17x improvement)

---

### Recommendation 2: Alternative - Use Sequential API Calls with Timer

Instead of persistent daemon, use a timer to call termux-sensor at regular intervals:

```python
def poll_sensor_threaded(self, interval_ms=50):
    """Poll sensor periodically at desired rate"""
    while not self.stop_event.is_set():
        try:
            # Single API call with explicit limit
            output = subprocess.run(
                ['termux-sensor', '-s', 'ACCELEROMETER', '-d', str(interval_ms), '-n', '1'],
                capture_output=True, text=True, timeout=2
            ).stdout

            # Parse and queue result
            data = json.loads(output)
            self.data_queue.put(extract_accel(data))
        except:
            pass

        time.sleep(interval_ms / 1000.0)
```

**Pros:**
- Each call only waits for one sample (not a continuous stream)
- No stream parsing complexity
- Exact timing control

**Cons:**
- Higher process spawning overhead
- More CPU usage than persistent daemon

---

### Recommendation 3: Benchmark Both Approaches

Create a comparison test:
1. **Persistent daemon with `-d 50`**: Should achieve ~17 Hz
2. **Sequential calls with timer**: Measure actual achieved rate
3. **Current implementation**: Baseline at ~1 Hz

Choose the approach that achieves 15+ Hz with lowest CPU usage.

---

## Why adb dumpsys Wasn't Available

The original suggestion to use `adb shell dumpsys sensorservice` doesn't apply to this setup:

1. **adb** requires connecting FROM a desktop TO the Android device
2. We're **already running ON the device** (Termux IS the Android shell)
3. Direct dumpsys access requires elevated privileges usually unavailable in Termux
4. termux-sensor is the **proper** Termux-approved interface for sensor access

`adb shell dumpsys sensorservice` would only be useful if:
- Running from a desktop PC connected via USB
- Trying to inspect global system sensor state
- Needing access to privileged Android APIs

---

## Technical Details: Where the 17.3 Hz Comes From

The LSM6DSO accelerometer chip (visible in JSON output: "lsm6dso LSM6DSO Accelerometer Non-wakeup") has configurable sample rates. The Android driver appears to be configured at:

- **17.3 Hz** (or possibly 16.67 Hz / 60 Hz configured with low-pass filter)
- This is a reasonable balance between:
  - Power consumption (lower sample rate = less power)
  - Data quality (enough samples for motion tracking)
  - Responsiveness (fast enough for user interactions)

The hardware supports higher rates (up to 800+ Hz in some modes), but Android is configured conservatively for power efficiency.

---

## Recommendations Summary

| Aspect | Recommendation | Expected Gain |
|--------|---|---|
| **Immediate fix** | Add `-d 50` to PersistentAccelDaemon | 1 Hz → ~17 Hz (17x) |
| **Verification** | Run benchmark test with new setting | Validate 15+ Hz achieved |
| **Fallback** | Consider sequential API approach if needed | Clean code, easier debugging |
| **Future** | Do NOT attempt adb dumpsys in Termux | Unavailable - use termux-sensor |

---

## Files Related to This Investigation

- `test_sensor_rates.sh` - Measures API call rates at different delays
- `analyze_sensor_data.py` - Determines actual hardware update frequency vs API overhead
- `SENSOR_POLLING_FINDINGS.md` - This file

## Next Steps

1. **Update motion_tracker_v2.py** to add `-d 50` parameter to termux-sensor
2. **Test for 1+ minutes** to verify stable 17 Hz rate
3. **Verify calibration works** at higher rate
4. **Check CPU usage** - should remain reasonable (<30%)
5. **Commit findings** once validated
