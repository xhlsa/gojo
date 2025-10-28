# Gyroscope Integration Guide for Motion Tracker V2

## Overview

This guide provides step-by-step instructions to integrate gyroscope support into `motion_tracker_v2.py`. The integration adds rotation detection capability to automatically trigger accelerometer recalibration when significant device orientation changes occur.

**Status:** Production-ready code ready for integration
**Location:** See `gyro_integration.py` for complete implementation
**Dependency:** `rotation_detector.py` (already present)

---

## Integration Steps

### Step 1: Add Import Statement

**File:** `motion_tracker_v2.py`
**Location:** Around line 20-25, with other imports

**Add this line:**
```python
from rotation_detector import RotationDetector
```

**Context (where to add):**
```python
import os
import sys
import threading
from queue import Queue, Empty
from datetime import datetime
from collections import deque
from statistics import mean
from rotation_detector import RotationDetector  # <-- ADD THIS LINE
```

---

### Step 2: Add PersistentGyroDaemon Class

**File:** `motion_tracker_v2.py`
**Location:** After `PersistentAccelDaemon` class (around line 416)
**Before:** `GPSThread` class

**Insert the entire `PersistentGyroDaemon` class from `gyro_integration.py`**

This class mirrors the accelerometer daemon pattern:
- Continuous stream reading from `termux-sensor -s GYROSCOPE -d 50`
- Queue-based data passing
- Brace-depth JSON parsing
- Thread-safe stop_event pattern

---

### Step 3: Modify MotionTrackerV2.__init__()

**File:** `motion_tracker_v2.py`
**Location:** Around line 748-750 (after `self.sensor_daemon = None`)

**Add these two lines:**
```python
        # Gyroscope daemon and rotation detector
        self.gyro_daemon = None
        self.rotation_detector = None
```

**Full context:**
```python
        # Sensor daemon (single long-lived process)
        self.sensor_daemon = None

        # Gyroscope daemon and rotation detector
        self.gyro_daemon = None
        self.rotation_detector = None

        # Threads
        self.gps_thread = None
```

---

### Step 4: Modify AccelerometerThread.__init__()

**File:** `motion_tracker_v2.py`
**Location:** Line 482 - method signature

**Change from:**
```python
    def __init__(self, accel_queue, stop_event, sensor_daemon, fusion=None, sample_rate=50, health_monitor=None):
```

**Change to:**
```python
    def __init__(self, accel_queue, stop_event, sensor_daemon, fusion=None, sample_rate=50,
                 health_monitor=None, gyro_daemon=None, rotation_detector=None):
```

**Add after line 489 (after `self.health_monitor = health_monitor`):**
```python
        # Gyroscope support (optional)
        self.gyro_daemon = gyro_daemon
        self.rotation_detector = rotation_detector

        # Rotation detection thresholds
        self.rotation_recal_threshold = 0.5  # radians (~28.6°)
        self.last_rotation_recal_time = None
        self.rotation_recal_interval = 5  # seconds - check every 5s
        self.last_rotation_magnitude = 0.0
```

---

### Step 5: Modify AccelerometerThread.run()

**File:** `motion_tracker_v2.py`
**Location:** Inside the main loop, after accel_data processing (around line 669-675)

**Add this gyroscope processing block after `accel_data = self.read_calibrated(raw_data)`:**

```python
                        accel_data = self.read_calibrated(raw_data)

                        # GYROSCOPE PROCESSING - If daemon available, read and process gyro data
                        if self.gyro_daemon and self.rotation_detector:
                            try:
                                # Non-blocking read from gyroscope daemon
                                gyro_raw = self.gyro_daemon.get_data(timeout=0.01)

                                if gyro_raw and accel_data:
                                    # Calculate dt since last accelerometer sample
                                    current_time = time.time()
                                    dt = current_time - (accel_data.get('timestamp', current_time) - 0.05)

                                    if dt > 0 and dt < 0.2:  # Valid dt range
                                        # Update rotation detector with gyroscope data
                                        self.rotation_detector.update_gyroscope(
                                            gyro_raw['x'],
                                            gyro_raw['y'],
                                            gyro_raw['z'],
                                            dt
                                        )

                                        # Check if rotation exceeded threshold
                                        rotation_state = self.rotation_detector.get_rotation_state()
                                        total_rotation_rad = rotation_state['total_rotation_radians']

                                        current_time_check = time.time()

                                        # Trigger recalibration if significant rotation detected
                                        if total_rotation_rad > self.rotation_recal_threshold:
                                            if (self.last_rotation_recal_time is None or
                                                (current_time_check - self.last_rotation_recal_time >= self.rotation_recal_interval)):

                                                rotation_degrees = rotation_state['total_rotation_degrees']
                                                primary_axis = rotation_state['primary_axis']

                                                print(f"⚡ [Rotation] Detected {rotation_degrees:.1f}° rotation "
                                                     f"(axis: {primary_axis}, threshold: {math.degrees(self.rotation_recal_threshold):.1f}°)")
                                                print(f"   Triggering accelerometer recalibration...")

                                                # Perform recalibration
                                                if self.fusion:
                                                    self.try_recalibrate(is_stationary=True)

                                                # Reset rotation angles after recalibration
                                                self.rotation_detector.reset_rotation_angles()
                                                self.last_rotation_recal_time = current_time_check

                                                print(f"   ✓ Recalibration complete, rotation angles reset")

                                        self.last_rotation_magnitude = total_rotation_rad

                            except Exception as e:
                                # Log gyro errors but don't interrupt accel processing
                                if not self.stop_event.is_set():
                                    if not hasattr(self, '_last_gyro_error_time'):
                                        self._last_gyro_error_time = time.time()
                                    elif time.time() - self._last_gyro_error_time > 10:
                                        print(f"\n⚠ Gyro processing error (continuing): {e}")
                                        self._last_gyro_error_time = time.time()

                        if accel_data:
                            try:
                                self.accel_queue.put_nowait(accel_data)
```

---

### Step 6: Modify MotionTrackerV2.start_threads()

**File:** `motion_tracker_v2.py`
**Location:** Around line 896-980

**Add gyroscope daemon startup after accelerometer daemon initialization:**

Replace this section (around line 926-927):
```python
        # Start GPS thread
        self.gps_thread = GPSThread(self.gps_queue, self.stop_event)
```

With this:
```python
        # Start gyroscope daemon (optional, non-critical)
        print("Starting gyroscope daemon...")
        try:
            self.gyro_daemon = PersistentGyroDaemon(delay_ms=50)
            if self.gyro_daemon.start():
                print("✓ Gyroscope daemon started")
                # Give daemon a moment to start producing data
                time.sleep(0.5)
            else:
                print("⚠ Gyroscope daemon failed to start (rotation detection disabled)")
                self.gyro_daemon = None
        except Exception as e:
            print(f"⚠ Failed to initialize gyroscope daemon: {e}")
            self.gyro_daemon = None

        # Start GPS thread
        self.gps_thread = GPSThread(self.gps_queue, self.stop_event)
```

**Then modify the pure Python accelerometer thread initialization:**

Change from (around line 965-974):
```python
            else:
                # ⚠ PURE PYTHON VERSION - Fallback if Cython unavailable
                self.accel_thread = AccelerometerThread(
                    self.accel_queue,
                    self.stop_event,
                    self.sensor_daemon,
                    fusion=self.fusion,
                    sample_rate=self.accel_sample_rate,
                    health_monitor=self.health_monitor
                )
                self.accel_thread.start()
```

To:
```python
            else:
                # ⚠ PURE PYTHON VERSION - Fallback if Cython unavailable
                # Initialize rotation detector if gyro available
                if self.gyro_daemon:
                    self.rotation_detector = RotationDetector(history_size=6000)
                    print(f"✓ RotationDetector initialized (history: 6000 samples)")

                self.accel_thread = AccelerometerThread(
                    self.accel_queue,
                    self.stop_event,
                    self.sensor_daemon,
                    fusion=self.fusion,
                    sample_rate=self.accel_sample_rate,
                    health_monitor=self.health_monitor,
                    gyro_daemon=self.gyro_daemon,
                    rotation_detector=self.rotation_detector
                )
                self.accel_thread.start()
```

---

### Step 7: Add Cleanup Code

**File:** `motion_tracker_v2.py`
**Location:** In `track()` method, cleanup section (around line 1227-1232)

**Add after `self.sensor_daemon.stop()` block:**
```python
        # Stop gyroscope daemon
        if self.gyro_daemon:
            try:
                self.gyro_daemon.stop()
                print("  ✓ Gyroscope daemon stopped")
            except Exception as e:
                print(f"⚠ Error stopping gyroscope daemon: {e}")
```

---

## Configuration Parameters

### PersistentGyroDaemon
- `delay_ms=50`: 50ms polling delay for ~20Hz hardware rate
- `max_queue_size=1000`: Queue size (matches accelerometer)

### RotationDetector
- `history_size=6000`: 60 seconds of history at 100Hz
- Records all rotation angles and magnitudes

### AccelerometerThread
- `rotation_recal_threshold=0.5`: Trigger recalibration at 0.5 radians (~28.6°)
- `rotation_recal_interval=5`: Check threshold every 5 seconds
- Prevents recalibration spam from repeated small rotations

---

## Behavior & Logging

### Startup Messages
```
✓ Gyroscope daemon started (20Hz, persistent stream)
   Process: termux-sensor (PID XXXXX)
✓ RotationDetector initialized (history: 6000 samples)
```

### During Tracking
When device is rotated >28.6°:
```
⚡ [Rotation] Detected 45.2° rotation (axis: y, threshold: 28.6°)
   Triggering accelerometer recalibration...
   ✓ Recalibration complete, rotation angles reset
```

### Errors (Non-blocking)
```
⚠ Failed to start gyroscope daemon: [error reason]
⚠ Gyroscope daemon failed to start (rotation detection disabled)
⚠ Gyro processing error (continuing): [error reason]
```

---

## Design Patterns Used

1. **Mirror Accelerometer Pattern**: PersistentGyroDaemon uses identical architecture to PersistentAccelDaemon
2. **Graceful Degradation**: Gyroscope is optional; entire system works without it
3. **Thread Safety**: stop_event pattern matches existing code
4. **Non-blocking Queue**: Uses get_nowait() to avoid blocking accelerometer thread
5. **Bounded History**: RotationDetector with fixed maxlen prevents memory leak
6. **Error Isolation**: Gyroscope errors don't interrupt accelerometer processing

---

## Testing Checklist

- [ ] Gyroscope daemon starts without crashing
- [ ] Rotation detected when phone is rotated
- [ ] Recalibration triggered correctly
- [ ] No impact on accelerometer performance
- [ ] Graceful degradation if gyroscope unavailable
- [ ] Proper cleanup on shutdown
- [ ] Memory usage remains bounded
- [ ] Works with both Cython and pure Python accel threads

---

## References

- `gyro_integration.py`: Complete implementation code
- `rotation_detector.py`: RotationDetector class documentation
- `PersistentAccelDaemon`: Template for daemon pattern (motion_tracker_v2.py line 266)

---

## Troubleshooting

### Gyroscope daemon won't start
```
⚠ Failed to start gyroscope daemon: [Errno 2] No such file or directory: 'termux-sensor'
```
**Solution:** Ensure `termux-sensor` is installed: `apt install termux-sensor`

### No rotation detected
1. Check daemon started: Look for "✓ Gyroscope daemon started"
2. Rotate phone >28.6° (default threshold)
3. Check logs for gyro processing errors
4. Verify `rotation_detector.py` is in same directory

### Excessive recalibrations
- Increase `rotation_recal_interval` from 5s to 10s
- Increase `rotation_recal_threshold` from 0.5 rad to 0.75 rad (~43°)

---

## Future Enhancements

1. Adaptive threshold based on GPS accuracy
2. Quaternion-based rotation for large angles >60°
3. Gyroscope bias calibration during stationary periods
4. Separate thread for RotationDetector (decouple from accel thread)
5. Persistence of calibration values to file
