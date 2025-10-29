# Incident Detection Guide

## Overview

The Motion Tracker V2 automatically detects and logs three types of driving incidents:

1. **Hard Braking** (>0.8g deceleration)
2. **Impacts** (>1.5g acceleration)
3. **Swerving** (>60°/sec rotation)

Each incident is saved with 30 seconds of context data **before** and **after** the event, including GPS location, acceleration, and gyroscope readings.

## Incident Types

### Hard Braking (>0.8g)

**What it detects:** Sudden deceleration events

**Typical causes:**
- Emergency stop
- Collision avoidance
- Sudden traffic stop

**Why it matters:** Proves you were braking, not accelerating into someone else

**Evidence captured:**
- Timestamp (GPS-accurate, atomic precision)
- Acceleration magnitude at event
- GPS location (accurate to ~5-10 meters)
- Speed before/after
- Vehicle rotation (proves if swerving during brake)

**Legal relevance:** Shows defensive driver response to emergency

### Impact (>1.5g)

**What it detects:** Collision or severe pothole/road condition

**Typical causes:**
- Collision
- Major pothole
- Railroad crossing
- Speed bump at high speed

**Why it matters:** Pinpoints exact moment and location of impact

**Evidence captured:**
- Precise timestamp of impact
- Magnitude of impact (1.5g = moderate, >2.5g = severe)
- GPS coordinates of exact location
- Vehicle motion before/after impact
- Acceleration pattern (sudden spike vs. gradual)

**Legal relevance:** Proves impact location and severity. Sudden spike = collision, gradual = road condition

### Swerving (>60°/sec)

**What it detects:** Sudden vehicle rotation/turning

**Typical causes:**
- Evasive action (avoiding obstacle)
- Loss of control
- Hard turn (but this is normal driving)

**Why it matters:** Shows you took evasive action; proves awareness

**Evidence captured:**
- Rotation speed (degrees/second)
- Duration of rotation
- Coinciding acceleration changes
- GPS movement during swerve

**Legal relevance:** Proves you attempted to avoid collision

## Data Quality & Accuracy

### Sensor Specifications

| Sensor | Range | Accuracy | Sampling Rate |
|--------|-------|----------|---------------|
| GPS | Global | ±5-10 meters | ~1 Hz |
| Accelerometer | ±16g | ±0.1g | 50 Hz |
| Gyroscope | ±2000°/s | ±1°/sec | 50 Hz |

### Kalman Filter Processing

All sensor data is processed through an **Extended Kalman Filter (EKF)**:

```
Raw sensors → EKF (fuses GPS + Accel + Gyro) → Filtered output

Benefits:
✓ Removes sensor noise while keeping true motion
✓ GPS + accel fusion: smooth velocity estimates
✓ Gyro reduces rotation noise
✓ Validates data (drops outliers)
✓ Numerically stable (Joseph form covariance)
```

**Filter validation:**
- Magnitude comparison: EKF vs Complementary Filter (baseline)
- Cross-validation on synthetic data
- Real-world testing on multiple routes

### Incident Logging Details

When an incident is detected:

1. **Event triggering:** Sensor reading exceeds threshold
2. **Context collection:** Automatically saves 30 seconds before + 30 seconds after
3. **Duplicate prevention:** 5-second cooldown (prevents logging same event multiple times)
4. **File creation:** `incident_TIMESTAMP_TYPE.json`
5. **Metadata:** Includes filter info, GPS accuracy, sensor status

### Example Incident File

```json
{
  "event_type": "hard_braking",
  "magnitude": 0.92,
  "timestamp": 1729718708.0799794,
  "datetime": "2025-10-28T23:18:28.079979",
  "context_seconds": 30,
  "threshold": 0.8,
  "accelerometer_samples": [
    {
      "timestamp": 1729718708.079,
      "magnitude": 0.15
    },
    ...
  ],
  "gyroscope_samples": [
    {
      "timestamp": 1729718708.079,
      "angular_velocity": 2.3
    },
    ...
  ],
  "gps_samples": [
    {
      "timestamp": 1729718708.079,
      "latitude": 37.7749,
      "longitude": -122.4194,
      "speed": 15.2,
      "accuracy": 5.0
    },
    ...
  ]
}
```

## How to Use

### Real-Time Logging

```bash
# Run motion tracker with incident detection enabled
python motion_tracker_v2/motion_tracker_v2.py 10

# Incidents auto-save to: motion_tracker_sessions/incidents/
```

### Accessing Incident Files

```bash
# List all incidents
ls motion_tracker_sessions/incidents/

# Examine a specific incident
cat motion_tracker_sessions/incidents/incident_*_impact.json | python3 -m json.tool
```

### Analyzing Incidents

```bash
# Get summary of incidents
python motion_tracker_v2/analyze_incidents.py

# Export incidents to CSV
python motion_tracker_v2/export_incidents.py --format csv --output incidents.csv
```

## Thresholds & Tuning

### Default Thresholds

| Event | Threshold | Reasoning |
|-------|-----------|-----------|
| Hard Braking | 0.8g | Distinguishes normal braking from emergency |
| Impact | 1.5g | Avoids false positives from bumps |
| Swerving | 60°/sec | ~2-3 second full rotation |

### Why These Values?

**Hard Braking (0.8g):**
- Normal city driving: 0.2-0.4g
- Highway braking: 0.5-0.7g
- Emergency: >0.8g ← Detection threshold
- Maximum safe: ~0.9g
- Race car: 1.5g+

**Impact (1.5g):**
- Small pothole: 1.0g
- Large pothole: 1.5g ← Detection threshold
- Minor collision: 2-3g
- Crash test: 10-15g

**Swerving (60°/sec):**
- Normal lane change: 30-50°/sec
- Evasive action: 60-120°/sec
- Limit before rollover: depends on vehicle

### Customization

Edit `incident_detector.py`:

```python
THRESHOLDS = {
    'hard_braking': 0.8,      # Lower = more sensitive
    'impact': 1.5,            # Raise to reduce false positives
    'swerving': 60.0,         # Higher = only extreme swerves
}
```

## Common Issues

### "False positives" (too many incidents logged)

**Cause:** Thresholds too low

**Solution:** Raise thresholds in `incident_detector.py`

```python
THRESHOLDS = {
    'hard_braking': 1.0,      # ↑ Was 0.8
    'impact': 2.0,            # ↑ Was 1.5
    'swerving': 80.0,         # ↑ Was 60
}
```

### "Missing incidents" (some events not logged)

**Cause:** Thresholds too high OR GPS accuracy poor

**Solutions:**
1. Lower thresholds
2. Check GPS signal (rural/tunnel areas have poor accuracy)
3. Verify accelerometer calibration

### "Timestamps don't match"

**Cause:** GPS timestamp vs accelerometer clock drift

**Solution:** Normal - GPS is atomic, accel is phone clock. EKF reconciles them.

### "Incident file incomplete"

**Cause:** Drive ended before 30 seconds of post-event data collected

**Solution:** Normal for end-of-drive incidents. Use available data (pre-event is still captured).

## For Insurance Disputes

### Preparation

1. **Regular testing:** Run the logger on typical drives
2. **Establish baseline:** Know your normal accel/braking patterns
3. **Keep calibration records:** Update before important drive
4. **Backup data:** Keep incidents folder synced to cloud

### After an Incident

1. **Don't delete anything:** Keep raw event files
2. **Export summary:** Create analysis report
3. **Include metadata:** Sensor specs, calibration date, filter info
4. **Note conditions:** Road type, weather, traffic
5. **Share with insurer:** Let them review with engineer

### What To Expect

- **Good case:** Clear incident file, strong context, smooth data
- **Weak case:** Missing context, noisy data, no GPS signal
- **Disputed:** Insurance may hire engineer to validate

## Technical Details

### Data Storage

Incidents are stored in JSON format for:
- ✓ Readability (human review)
- ✓ Portability (any system can read)
- ✓ Transparency (no proprietary format)
- ✓ Extensibility (easy to add fields)

### Redundancy

To preserve incident data:

```bash
# Backup incidents folder
cp -r motion_tracker_sessions/incidents/ ~/backups/incidents_$(date +%Y%m%d)/

# Or sync to cloud
rclone sync motion_tracker_sessions/incidents/ drive:gojo_incidents/
```

## Next Steps

- Test on your regular routes (establish baseline)
- Compare incident detection results with your driving memory
- Adjust thresholds if needed
- Create calibration certificate before important drives
- Keep incidents in secure backup

---

**Questions?** See `LEGAL_USE.md` for using incidents as evidence, or `CALIBRATION.md` for sensor validation details.
