# RotationDetector - Quick Reference

## Quick Start

```python
from rotation_detector import RotationDetector

# Create detector
detector = RotationDetector(history_size=6000)

# Feed gyroscope data
success = detector.update_gyroscope(gyro_x, gyro_y, gyro_z, dt)
if success:
    state = detector.get_rotation_state()
    print(f"Rotation: {state['total_rotation_degrees']:.1f}°")
    print(f"Primary axis: {state['primary_axis']}")

# Reset when done
detector.reset_all()
```

## Key Features

| Feature | Method | Returns |
|---------|--------|---------|
| Update gyro | `update_gyroscope(x, y, z, dt)` | bool |
| Get angles | `get_rotation_state()` | dict |
| Get history | `get_rotation_history()` | list |
| Primary axis | `get_axis_dominance()` | str |
| Reset angles | `reset_rotation_angles()` | - |
| Full reset | `reset_all()` | - |
| Diagnostics | `get_diagnostics()` | dict |

## Sonnet Priorities - All Implemented ✓

### Priority 1 (MUST FIX)
1. ✓ **Angle normalization [-π, π]** - Automatic in `update_gyroscope()`
2. ✓ **Reset method names** - `reset_rotation_angles()` vs `reset_all()`
3. ✓ **Skip large dt** - Skips samples with dt > 100ms (not clamped)

### Priority 2 (SHOULD FIX)
4. ✓ **LIMITATIONS docstring** - Documents >60° rotation accuracy loss
5. ✓ **Input validation** - Converts to float, validates dt > 0
6. ✓ **Integration example** - Full code in class docstring + Termux API example

## Rotation State Dictionary

```python
state = detector.get_rotation_state()
# Returns:
{
    'angle_pitch': 0.05,              # rad, X-axis
    'angle_roll': -0.03,              # rad, Y-axis
    'angle_yaw': 0.12,                # rad, Z-axis
    'angle_pitch_degrees': 2.87,      # converted
    'angle_roll_degrees': -1.72,      # converted
    'angle_yaw_degrees': 6.88,        # converted
    'total_rotation_radians': 0.139,  # magnitude
    'total_rotation_degrees': 7.96,   # degrees
    'primary_axis': 'z',              # which axis moved most
    'sample_count': 42                # total samples processed
}
```

## AccelerometerThread Integration Template

```python
# In AccelerometerThread.__init__:
from rotation_detector import RotationDetector
self.rotation_detector = RotationDetector(history_size=6000)

# In AccelerometerThread.run():
import subprocess
import json

gyro_result = subprocess.run(
    ['termux-sensor', '-s', 'GYROSCOPE', '-l', '1'],
    capture_output=True,
    text=True,
    timeout=2
)
if gyro_result.returncode == 0:
    gyro_data = json.loads(gyro_result.stdout)
    values = gyro_data['GYROSCOPE']['values']

    # Update detector (values in rad/s)
    success = self.rotation_detector.update_gyroscope(values[0], values[1], values[2], dt)

    if success:
        state = self.rotation_detector.get_rotation_state()

        # Trigger recalibration on significant rotation
        if state['total_rotation_degrees'] > 30:
            print(f"⚡ Detected {state['total_rotation_degrees']:.1f}° rotation, recalibrating...")
            self.try_recalibrate(is_stationary=True)
            self.rotation_detector.reset_all()
```

## Method Behavior

### update_gyroscope()
```python
success = detector.update_gyroscope(0.5, 0.2, 0.1, 0.01)
# Returns: True if processed, False if:
#   - Invalid input type → logged as WARNING
#   - dt <= 0 → logged as WARNING
#   - dt > 100ms → logged as DEBUG, skipped
```

### reset_rotation_angles()
```python
detector.reset_rotation_angles()
# Zeros angles only: pitch=0, roll=0, yaw=0
# PRESERVES: rotation_history, axis_contributions
# Use: After orientation change, before measuring next rotation
```

### reset_all()
```python
detector.reset_all()
# Zeros: angles, history, axis stats, counters
# CLEARS: Everything
# Use: At end of tracking session or full state reset needed
```

## Configuration

```python
# Default (6000 samples = 60s @ 100Hz)
detector = RotationDetector()

# Custom history size (3000 = 30s @ 100Hz)
detector = RotationDetector(history_size=3000)

# Change dt threshold after creation
detector.max_dt = 0.05  # 50ms instead of 100ms

# Get diagnostics
diag = detector.get_diagnostics()
print(f"Processed {diag['sample_count']} samples")
print(f"Skipped {diag['skipped_large_dt_count']} samples (dt > 100ms)")
print(f"History: {diag['history_size']}/{diag['history_capacity']}")
```

## Limitations

- Designed for rotations < 60°, accuracy degrades for larger rotations
- No gyroscope bias drift compensation (manual recalibration recommended)
- Assumes constant rotation rate during each dt interval

## File Location

`/data/data/com.termux/files/home/gojo/motion_tracker_v2/rotation_detector.py`

## Status

✓ Production Ready
✓ All Sonnet Recommendations Implemented
✓ Tested and Verified
✓ Ready for Integration
