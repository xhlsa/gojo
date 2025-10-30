# Uncalibrated Sensor Stream Testing

**Date:** Oct 30, 2025  
**Objective:** Test if uncalibrated sensor streams with `-d 0` delay can bypass the 15 Hz limit

---

## Test Results

### Findings

1. **Uncalibrated sensors are listed but non-functional**
   ```
   Available:
   - "lsm6dso LSM6DSO Accelerometer-Uncalibrated Non-wakeup"
   - "lsm6dso LSM6DSO Gyroscope-Uncalibrated Non-wakeup"
   
   Status: Listed in termux-sensor -l but return NO DATA
   ```

2. **Testing uncalibrated streams**
   - Command: `termux-sensor -s "Accelerometer-Uncalibrated" -d 0` through `-d 50`
   - Result: 0 Hz on all delay settings (no data returned)
   - Comparison: Standard calibrated ACCELEROMETER works normally

3. **Calibrated accelerometer at aggressive delays**
   - `-d 50`: ✓ Works (~15 Hz)
   - `-d 20`: ✓ Works (~15 Hz)
   - `-d 10`: ✓ Works (~15 Hz)
   - `-d 5`: ✓ Works (~15 Hz)
   - `-d 1`: ✓ Works, may trigger API limits
   - `-d 0`: Previously tested - triggers API overload (Connection refused)

---

## Conclusion

**Uncalibrated sensor streams do not provide a workaround for the 15 Hz limit.**

### Why Uncalibrated Sensors Return No Data

The uncalibrated sensor mode is listed in the sensor capabilities but doesn't produce actual output. Possible reasons:

1. **Device implementation:** Samsung LSM6DSO sensor driver may not expose uncalibrated mode through Termux:API
2. **Firmware limitation:** The IMU firmware may not support independent uncalibrated data streaming
3. **Termux:API version:** Current Termux:API (v0.53.0) may not implement uncalibrated sensor access properly
4. **System configuration:** The Android sensor framework may be configured to skip uncalibrated streams

### Technical Notes

- Uncalibrated sensors **are** available on the hardware (listed in `termux-sensor -l`)
- But they **don't stream** through Termux:API (returns empty data)
- Suggests either firmware-level or Termux:API-level limitation, not a Termux issue
- The frequency ceiling (15 Hz) remains unchanged regardless of calibrated/uncalibrated choice

---

## Recommendation

**Continue with calibrated accelerometer at current settings (delay_ms=50).**

The uncalibrated stream approach does not yield higher frequencies because:
1. The hardware doesn't expose uncalibrated data through this API
2. The 15 Hz limit is still in effect for any data that IS returned
3. No bypass available through this method

The 15 Hz architectural limit stands regardless of sensor calibration mode.

---

## Files Referenced
- SENSOR_ACCESS_EXPLORATION.md - Full technical alternatives analysis
- COMMUNITY_RESEARCH_FINDINGS.md - What the developer community found
- ACCELEROMETER_ROOT_CAUSE.md - Root cause of the 15 Hz limit

