# Gyroscope Integration - Production-Ready Code

This document contains all code sections ready for direct insertion into `motion_tracker_v2.py`.

## File: motion_tracker_v2.py

### SECTION A: PersistentGyroDaemon Class

**INSERT LOCATION:** After `PersistentAccelDaemon` class ends (around line 416)
**BEFORE:** `GPSThread` class definition
**LINES:** Approximately 200 lines

```python
class PersistentGyroDaemon:
    """
    Persistent gyroscope daemon - starts termux-sensor ONCE and reads continuously.

    Mirrors PersistentAccelDaemon pattern:
    - Single long-lived termux-sensor process
    - Continuous JSON stream with brace-depth parsing
    - Queue-based data passing to AccelerometerThread
    - Thread-safe with stop_event

    The gyroscope provides angular velocity data (rad/s) that, when integrated,
    gives absolute rotation angles. Used by RotationDetector to detect device
    orientation changes that trigger accelerometer recalibration.
    """

    def __init__(self, delay_ms=50, max_queue_size=1000):
        """
        Initialize gyroscope daemon.

        Args:
            delay_ms (int): Polling delay in milliseconds (50ms = ~20Hz sampling)
            max_queue_size (int): Maximum queue depth before dropping samples
        """
        self.delay_ms = delay_ms
        self.data_queue = Queue(maxsize=max_queue_size)
        self.reader_thread = None
        self.stop_event = threading.Event()
        self.sensor_process = None

    def start(self):
        """Start persistent termux-sensor daemon for gyroscope"""
        try:
            # Start termux-sensor with same parameters as accelerometer
            # -s GYROSCOPE: Select gyroscope sensor
            # -d 50: 50ms polling delay for ~20Hz hardware rate
            # stdbuf -oL: Line-buffered output (one JSON object per line)
            self.sensor_process = subprocess.Popen(
                ['stdbuf', '-oL', 'termux-sensor', '-s', 'GYROSCOPE', '-d', '50'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                bufsize=1  # Line buffered
            )

            # Verify process started
            if self.sensor_process.poll() is not None:
                stderr_out = self.sensor_process.stderr.read() if self.sensor_process.stderr else ""
                raise RuntimeError(f"termux-sensor GYROSCOPE exited immediately: {stderr_out}")

            # Start reader thread
            self.reader_thread = threading.Thread(target=self._read_loop, daemon=True)
            self.reader_thread.start()

            delay_hz = 1000 // self.delay_ms
            print(f"✓ Gyroscope daemon started ({delay_hz:.0f}Hz, persistent stream)")
            print(f"   Process: termux-sensor (PID {self.sensor_process.pid})")
            return True
        except Exception as e:
            print(f"⚠ Failed to start gyroscope daemon: {e}")
            return False

    def _read_loop(self):
        """Read JSON objects from persistent gyroscope stream (multi-line formatted)"""
        try:
            if not self.sensor_process or not self.sensor_process.stdout:
                print("⚠ [GyroDaemon] No stdout from sensor process", file=sys.stderr)
                return

            json_buffer = ""
            brace_depth = 0
            line_count = 0

            for line in self.sensor_process.stdout:
                if self.stop_event.is_set():
                    break

                if not line:
                    continue

                line_count += 1
                json_buffer += line + '\n'

                # Track brace depth to detect complete JSON objects
                brace_depth += line.count('{') - line.count('}')

                # When brace_depth returns to 0, we have a complete JSON object
                if brace_depth == 0 and '{' in json_buffer and json_buffer.count('}') > 0:
                    try:
                        # Parse the complete JSON object
                        data = json_loads(json_buffer)
                        json_buffer = ""

                        # Extract gyroscope values from any sensor key
                        for sensor_key, sensor_data in data.items():
                            if isinstance(sensor_data, dict) and 'values' in sensor_data:
                                values = sensor_data['values']
                                if len(values) >= 3:
                                    gyro_data = {
                                        'x': values[0],  # rad/s around X-axis (pitch)
                                        'y': values[1],  # rad/s around Y-axis (roll)
                                        'z': values[2],  # rad/s around Z-axis (yaw)
                                        'timestamp': time.time()
                                    }

                                    # Try to put in queue, skip if full
                                    try:
                                        self.data_queue.put_nowait(gyro_data)
                                    except:
                                        # Queue full, skip this sample
                                        pass
                                    break  # Only process first sensor

                    except (ValueError, KeyError, IndexError, TypeError):
                        # Skip malformed JSON, continue buffering
                        json_buffer = ""
                        brace_depth = 0

        except Exception as e:
            print(f"⚠ [GyroDaemon] Reader thread error: {e}", file=sys.stderr)
        finally:
            # Clean up process
            if self.sensor_process:
                try:
                    self.sensor_process.terminate()
                    self.sensor_process.wait(timeout=1)
                except:
                    try:
                        self.sensor_process.kill()
                    except:
                        pass

    def stop(self):
        """Stop the daemon"""
        self.stop_event.set()

        # Kill sensor process if running
        if self.sensor_process:
            try:
                self.sensor_process.terminate()
                self.sensor_process.wait(timeout=1)
            except:
                try:
                    self.sensor_process.kill()
                except:
                    pass

        # Wait for reader thread
        if self.reader_thread:
            self.reader_thread.join(timeout=2)

    def __del__(self):
        """Ensure cleanup if daemon is garbage collected without explicit stop()"""
        try:
            self.stop()
        except:
            pass  # Silently ignore errors during cleanup

    def get_data(self, timeout=None):
        """Get next gyroscope reading from daemon"""
        try:
            return self.data_queue.get(timeout=timeout)
        except Empty:
            return None
```

---

### SECTION B: Modification to MotionTrackerV2.__init__()

**INSERT LOCATION:** Around line 748, after `self.sensor_daemon = None`

```python
        # Gyroscope daemon and rotation detector
        self.gyro_daemon = None
        self.rotation_detector = None
```

---

### SECTION C: Modification to AccelerometerThread.__init__() signature

**LOCATION:** Line 482

**CHANGE FROM:**
```python
    def __init__(self, accel_queue, stop_event, sensor_daemon, fusion=None, sample_rate=50, health_monitor=None):
```

**CHANGE TO:**
```python
    def __init__(self, accel_queue, stop_event, sensor_daemon, fusion=None, sample_rate=50,
                 health_monitor=None, gyro_daemon=None, rotation_detector=None):
```

---

### SECTION D: Modification to AccelerometerThread.__init__() body

**INSERT LOCATION:** After line 489, after `self.health_monitor = health_monitor`

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

### SECTION E: Gyroscope Processing in AccelerometerThread.run()

**INSERT LOCATION:** After `accel_data = self.read_calibrated(raw_data)` (around line 669)
**BEFORE:** The `if accel_data:` block that puts data in queue

```python
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
```

---

### SECTION F: Gyroscope Daemon Startup in start_threads()

**INSERT LOCATION:** Around line 926, before `self.gps_thread = GPSThread(...)`
**AFTER:** The accelerometer daemon startup and validation

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
```

---

### SECTION G: Rotation Detector Initialization

**INSERT LOCATION:** Around line 965-974, in the pure Python accelerometer thread initialization
**BEFORE:** Creating `AccelerometerThread` instance

**REPLACE THIS:**
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
```

**WITH THIS:**
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
```

---

### SECTION H: Gyroscope Cleanup in track()

**INSERT LOCATION:** Around line 1228, after `self.sensor_daemon.stop()` block
**BEFORE:** The "Kill any lingering termux-sensor" comment

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

### SECTION I: Import Statement (TOP OF FILE)

**INSERT LOCATION:** Around line 20-25, with other imports

**ADD THIS LINE:**
```python
from rotation_detector import RotationDetector
```

---

## Summary of Changes

| Section | Type | Location | Lines |
|---------|------|----------|-------|
| A | New Class | After PersistentAccelDaemon | ~200 |
| B | Instance vars | MotionTrackerV2.__init__ | 2 |
| C | Method signature | AccelerometerThread.__init__ line 482 | 2 |
| D | Method body | AccelerometerThread.__init__ after line 489 | 8 |
| E | Main loop code | AccelerometerThread.run() after line 669 | ~50 |
| F | Startup code | start_threads() before GPS thread | ~10 |
| G | Initialization | start_threads() before creating accel thread | ~6 |
| H | Cleanup code | track() method cleanup section | ~7 |
| I | Import | Top of file with other imports | 1 |

**Total additions:** ~286 lines

---

## Integration Order

1. Add import statement (Section I)
2. Add PersistentGyroDaemon class (Section A)
3. Update MotionTrackerV2.__init__() (Section B)
4. Update AccelerometerThread.__init__() signature (Section C)
5. Update AccelerometerThread.__init__() body (Section D)
6. Add gyroscope processing to run() (Section E)
7. Add gyroscope startup to start_threads() (Section F)
8. Add rotation detector initialization (Section G)
9. Add cleanup code (Section H)

---

## Verification Checklist

After integration:

- [ ] Import statement added to top
- [ ] PersistentGyroDaemon class present
- [ ] MotionTrackerV2 has gyro_daemon and rotation_detector attributes
- [ ] AccelerometerThread accepts new parameters
- [ ] Gyroscope processing block in run() method
- [ ] Gyroscope daemon startup in start_threads()
- [ ] Rotation detector initialization present
- [ ] Cleanup code in track() method

---

## Testing

```bash
# Run with gyroscope support
python motion_tracker_v2/motion_tracker_v2.py 5

# Expected output:
# ✓ Gyroscope daemon started (20Hz, persistent stream)
# ✓ RotationDetector initialized (history: 6000 samples)
#
# ... rotate phone >28.6° ...
#
# ⚡ [Rotation] Detected 45.2° rotation (axis: y, threshold: 28.6°)
#    Triggering accelerometer recalibration...
#    ✓ Recalibration complete, rotation angles reset
```

---

## Files Modified

- `motion_tracker_v2.py` - Main integration (8 modification points)
- No other files need modification
- `rotation_detector.py` is already present and functional
