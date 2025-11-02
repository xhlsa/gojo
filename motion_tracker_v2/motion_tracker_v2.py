#!/usr/bin/env python3
"""
GPS + Accelerometer Sensor Fusion Tracker V2 - Multithreaded Edition
Continuous sensor streaming with background threads for maximum data capture

⚠️  MANDATORY: ALWAYS RUN VIA SHELL SCRIPT, NOT DIRECTLY
================================================================================
WRONG:  python motion_tracker_v2.py 5
RIGHT:  ./motion_tracker_v2.sh 5

The shell script (./motion_tracker_v2.sh) is REQUIRED because it:
  1. Cleans up stale sensor processes before startup
  2. Validates accelerometer is accessible (with retry logic)
  3. Ensures proper sensor initialization
  4. Handles signal cleanup on exit

Running this directly will fail with sensor initialization errors.
================================================================================

RECENT OPTIMIZATION (Oct 27):
- Added -d 50 parameter to termux-sensor for optimal polling rate
- Hardware provides ~15-17 Hz actual rate (LSM6DSO accelerometer)
- Default accel_sample_rate changed from 50 Hz → 16 Hz (hardware reality)
- Eliminates wasted API calls that only returned cached values
- See tools/SENSOR_POLLING_FINDINGS.md for detailed analysis and benchmarks
"""

import subprocess
import gzip
import time
import math
import signal
import os
import sys
import threading
from queue import Queue, Empty
from datetime import datetime
from collections import deque
from statistics import mean

# Try orjson (faster C-based JSON) with graceful fallback to stdlib json
try:
    import orjson
    HAS_ORJSON = True
except ImportError:
    HAS_ORJSON = False
    import json

# Import rotation detector for gyroscope-based recalibration
from rotation_detector import RotationDetector

# Import sensor fusion filters
from filters import get_filter

# JSON compatibility helpers (unified interface for orjson and json)
def json_loads(s):
    """Load JSON from string, using orjson if available."""
    if HAS_ORJSON:
        return orjson.loads(s)
    else:
        return json.loads(s)

def json_dump(obj, fp, **kwargs):
    """Dump JSON to file, using orjson if available."""
    if HAS_ORJSON:
        indent = kwargs.get('indent', None)
        option = 0
        if indent:
            option |= orjson.OPT_INDENT_2
        data = orjson.dumps(obj, option=option)
        if isinstance(data, bytes):
            fp.write(data.decode('utf-8') if not hasattr(fp, 'mode') or 'b' not in fp.mode else data)
        else:
            fp.write(data)
    else:
        json.dump(obj, fp, **kwargs)

def json_decode_error():
    """Return the appropriate JSONDecodeError class."""
    if HAS_ORJSON:
        return orjson.JSONDecodeError
    else:
        return json.JSONDecodeError

# Optional: psutil for battery monitoring (not critical for benchmarking)
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

# Ensure sessions directory exists (date-based organization for multi-session clarity)
# Sessions organized by date: sessions/YYYY-MM-DD_description/
from datetime import datetime
current_date = datetime.now().strftime("%Y-%m-%d")
# Auto-detect session folder or fall back to "2025-10-27_accel_fix" format
session_base = os.path.join(os.path.dirname(__file__), "..", "sessions")
# Try to find today's session folder, or use archive
session_subdirs = []
if os.path.exists(session_base):
    session_subdirs = [d for d in os.listdir(session_base) if os.path.isdir(os.path.join(session_base, d)) and d.startswith(current_date.replace("-", "-"))]

if session_subdirs:
    SESSIONS_DIR = os.path.join(session_base, session_subdirs[0])
else:
    # Fall back to current session folder (create if needed)
    SESSIONS_DIR = os.path.join(session_base, f"{current_date.replace('-', '-')}_session")

os.makedirs(SESSIONS_DIR, exist_ok=True)

# Try to import Cython-optimized accelerometer processor
try:
    from accel_processor import FastAccelProcessor
    HAS_CYTHON = True
except ImportError:
    HAS_CYTHON = False

# Import health monitoring and acceleration calculation
try:
    from accel_health_monitor import AccelHealthMonitor
    HAS_HEALTH_MONITOR = True
except ImportError:
    HAS_HEALTH_MONITOR = False
    print("⚠ AccelHealthMonitor not available (continuing without detailed diagnostics)")

try:
    from accel_calculator import AccelerationCalculator
    HAS_ACCEL_CALC = True
except ImportError:
    HAS_ACCEL_CALC = False


class PersistentAccelDaemon:
    """
    Persistent accelerometer daemon - starts termux-sensor ONCE and reads continuously

    Key insight: Instead of start/stop for each sample (1.5s init delay each time),
    keep the process running and read JSON objects from continuous stream.

    This achieves true ~100+ Hz sampling vs ~0.66 Hz with repeated script calls.
    """

    def __init__(self, delay_ms=20, max_queue_size=1000):
        self.delay_ms = delay_ms
        self.data_queue = Queue(maxsize=max_queue_size)
        self.gyro_queue = Queue(maxsize=max_queue_size)  # For gyroscope data from paired sensor
        self.reader_thread = None
        self.stop_event = threading.Event()
        self.sensor_process = None

    def start(self):
        """Start persistent termux-sensor daemon"""
        try:
            # Start termux-sensor as persistent process with line-buffered output
            # Request both ACCELEROMETER and GYROSCOPE together (same hardware sensor)
            # stdbuf -oL forces line-buffering (one JSON object per line)
            # -d 50 sets 50ms polling delay for ~17Hz hardware rate (vs default ~1Hz)
            self.sensor_process = subprocess.Popen(
                ['stdbuf', '-oL', 'termux-sensor', '-s', 'ACCELEROMETER,GYROSCOPE', '-d', '50'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                bufsize=1  # Line buffered
            )

            # Verify process started
            if self.sensor_process.poll() is not None:
                stderr_out = self.sensor_process.stderr.read() if self.sensor_process.stderr else ""
                raise RuntimeError(f"termux-sensor exited immediately: {stderr_out}")

            # Start reader thread that reads from continuous stream
            self.reader_thread = threading.Thread(target=self._read_loop, daemon=True)
            self.reader_thread.start()

            delay_hz = 1000 // self.delay_ms
            print(f"✓ IMU daemon started ({delay_hz:.0f}Hz, persistent paired stream)")
            print(f"   Sensors: ACCELEROMETER + GYROSCOPE (same hardware)")
            print(f"   Process: termux-sensor (PID {self.sensor_process.pid})")
            return True
        except Exception as e:
            print(f"⚠ Failed to start accelerometer daemon: {e}")
            return False

    def _read_loop(self):
        """Read JSON objects from persistent sensor stream (multi-line formatted)"""
        try:
            if not self.sensor_process or not self.sensor_process.stdout:
                print("⚠ [AccelDaemon] No stdout from sensor process", file=sys.stderr)
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
                        # Parse the complete JSON object (contains both accel and gyro from paired sensors)
                        data = json_loads(json_buffer)
                        json_buffer = ""

                        # Extract both accelerometer and gyroscope from the combined JSON
                        for sensor_key, sensor_data in data.items():
                            if isinstance(sensor_data, dict) and 'values' in sensor_data:
                                values = sensor_data['values']
                                if len(values) >= 3:
                                    # Determine sensor type from key name
                                    if 'Accelerometer' in sensor_key:
                                        accel_data = {
                                            'x': values[0],
                                            'y': values[1],
                                            'z': values[2],
                                            'timestamp': time.time()
                                        }
                                        # Try to put in accel queue
                                        try:
                                            self.data_queue.put_nowait(accel_data)
                                        except:
                                            pass  # Queue full, skip this sample

                                    elif 'Gyroscope' in sensor_key:
                                        gyro_data = {
                                            'x': values[0],  # rad/s around X-axis (pitch)
                                            'y': values[1],  # rad/s around Y-axis (roll)
                                            'z': values[2],  # rad/s around Z-axis (yaw)
                                            'timestamp': time.time()
                                        }
                                        # Try to put in gyro queue
                                        try:
                                            self.gyro_queue.put_nowait(gyro_data)
                                        except:
                                            pass  # Queue full, skip this sample

                    except (ValueError, KeyError, IndexError, TypeError):
                        # Skip malformed JSON, continue buffering
                        json_buffer = ""
                        brace_depth = 0

        except Exception as e:
            print(f"⚠️  [AccelDaemon] Reader thread error: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc(file=sys.stderr)
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

    def is_alive(self):
        """Check if daemon subprocess is still running"""
        if not self.sensor_process:
            return False
        poll_result = self.sensor_process.poll()
        # poll() returns None if process is still running, non-None if it has exited
        return poll_result is None

    def get_status(self):
        """Get daemon status for debugging"""
        if not self.sensor_process:
            return "NOT_STARTED"
        if not self.is_alive():
            exit_code = self.sensor_process.poll()
            return f"DEAD (exit_code={exit_code})"
        return "ALIVE"

    def __del__(self):
        """Ensure cleanup if daemon is garbage collected without explicit stop()"""
        try:
            self.stop()
        except:
            pass  # Silently ignore errors during cleanup

    def get_data(self, timeout=None):
        """Get next sensor reading from daemon"""
        try:
            return self.data_queue.get(timeout=timeout)
        except Empty:
            return None


class PersistentGyroDaemon:
    """
    Gyroscope data reader - gets data from paired PersistentAccelDaemon stream.

    IMPORTANT: Accelerometer and gyroscope are from the same IMU hardware,
    so they MUST be initialized together using `termux-sensor -s ACCELEROMETER,GYROSCOPE`.

    This class provides a wrapper interface to access gyroscope data that's already
    being read from the paired sensor stream in PersistentAccelDaemon.

    The gyroscope provides angular velocity data (rad/s) that, when integrated,
    gives absolute rotation angles. Used by RotationDetector to detect device
    orientation changes that trigger accelerometer recalibration.
    """

    def __init__(self, accel_daemon=None, delay_ms=50, max_queue_size=1000):
        """
        Initialize gyroscope daemon (wrapper around shared paired sensor).

        Args:
            accel_daemon: PersistentAccelDaemon instance (provides shared gyro_queue)
            delay_ms (int): Polling delay in milliseconds (for reference only)
            max_queue_size (int): Maximum queue depth before dropping samples
        """
        self.delay_ms = delay_ms
        self.accel_daemon = accel_daemon
        # Use the accel daemon's gyro queue if available, else create our own
        self.data_queue = accel_daemon.gyro_queue if accel_daemon else Queue(maxsize=max_queue_size)
        self.stop_event = threading.Event()

    def start(self):
        """Start reading from shared gyroscope stream (no process to start)"""
        try:
            if self.accel_daemon is None:
                print("⚠ GyroDaemon: No accel_daemon provided, cannot read gyro data")
                return False

            delay_hz = 1000 // self.delay_ms
            print(f"✓ Gyroscope daemon started ({delay_hz:.0f}Hz, paired with accelerometer)")
            print(f"   Source: Shared IMU sensor stream (ACCELEROMETER,GYROSCOPE)")
            return True
        except Exception as e:
            print(f"⚠ Failed to initialize gyroscope daemon: {e}")
            return False

    def stop(self):
        """Stop the daemon (no process to kill, just set flag)"""
        self.stop_event.set()

    def __del__(self):
        """Ensure cleanup if daemon is garbage collected without explicit stop()"""
        try:
            self.stop()
        except:
            pass

    def get_data(self, timeout=None):
        """Get next gyroscope reading from shared daemon"""
        try:
            return self.data_queue.get(timeout=timeout)
        except Empty:
            return None


class GPSThread(threading.Thread):
    """Background thread for continuous GPS polling"""

    def __init__(self, gps_queue, stop_event):
        super().__init__(daemon=True)
        self.gps_queue = gps_queue
        self.stop_event = stop_event
        self.update_interval = 1.0  # Try to update every second

    def read_gps(self, timeout=15):
        """Read GPS data from Termux API"""
        try:
            result = subprocess.run(
                ['termux-location', '-p', 'gps'],
                capture_output=True,
                text=True,
                timeout=timeout
            )

            if result.returncode == 0 and result.stdout:
                data = json_loads(result.stdout)
                return {
                    'latitude': data.get('latitude'),
                    'longitude': data.get('longitude'),
                    'altitude': data.get('altitude'),
                    'speed': data.get('speed'),  # m/s
                    'bearing': data.get('bearing'),
                    'accuracy': data.get('accuracy'),
                    'timestamp': time.time()
                }
        except Exception as e:
            return None

        return None

    def run(self):
        """Continuously poll GPS"""
        try:
            while not self.stop_event.is_set():
                try:
                    gps_data = self.read_gps()

                    if gps_data and gps_data.get('latitude'):
                        self.gps_queue.put(gps_data)

                    # Wait before next poll (if not stopping)
                    self.stop_event.wait(self.update_interval)

                except Exception as e:
                    # Log error but continue running
                    if not self.stop_event.is_set():
                        print(f"\n⚠ GPS thread error (continuing): {e}")
                    time.sleep(1)

        except Exception as e:
            # Fatal error - thread will die
            print(f"\n⚠ GPS thread FATAL error: {e}")
            import traceback
            traceback.print_exc()


class AccelerometerThread(threading.Thread):
    """Background thread for reading from sensor daemon"""

    def __init__(self, accel_queue, stop_event, sensor_daemon, fusion=None, sample_rate=50,
                 health_monitor=None, gyro_daemon=None, rotation_detector=None):
        super().__init__(daemon=True)
        self.accel_queue = accel_queue
        self.stop_event = stop_event
        self.sensor_daemon = sensor_daemon
        self.fusion = fusion  # Reference to fusion for stationary detection
        self.sample_rate = sample_rate  # Hz
        self.health_monitor = health_monitor  # Health monitoring

        # Gyroscope support (optional)
        self.gyro_daemon = gyro_daemon
        self.rotation_detector = rotation_detector

        # Rotation detection thresholds
        self.rotation_recal_threshold = 0.5  # radians (~28.6°)
        self.last_rotation_recal_time = None
        self.rotation_recal_interval = 5  # seconds - check every 5s
        self.last_rotation_magnitude = 0.0

        # Calibration
        self.bias = {'x': 0.0, 'y': 0.0, 'z': 0.0}
        self.gravity = 9.8
        self.calibrated = False

        # Acceleration calculator (handles device tilt correctly)
        self.accel_calc = None

        # Dynamic re-calibration (when stationary)
        self.last_recal_time = None
        self.recal_interval = 30  # seconds - check every 30s if stationary
        self.recal_buffer = []
        self.recal_threshold = 0.5  # buffer count before recalibrating

    def calibrate(self, samples=10, silent=False, health_monitor=None):
        """Calibrate accelerometer bias from daemon readings (magnitude-based)"""
        if not silent:
            print("Calibrating accelerometer (keep device still)...")
        calibration_samples = []

        # Try to collect samples with retries if daemon is slow
        retry_count = 0
        max_retries = 2

        for attempt in range(max_retries + 1):
            calibration_samples = []
            for _ in range(samples):
                raw = self.sensor_daemon.get_data(timeout=1)
                if raw:
                    calibration_samples.append(raw)
                time.sleep(0.2)

            # If we got enough samples, we're done
            if calibration_samples:
                break

            # If no samples and more retries available, wait and try again
            if attempt < max_retries and not calibration_samples:
                if not silent:
                    print(f"  Retrying daemon communication (attempt {attempt + 2}/{max_retries + 1})...")
                time.sleep(1)

        if calibration_samples:
            # Store raw axis biases (used for offset removal)
            self.bias['x'] = mean(s['x'] for s in calibration_samples)
            self.bias['y'] = mean(s['y'] for s in calibration_samples)
            self.bias['z'] = mean(s['z'] for s in calibration_samples)

            # Calculate gravity magnitude (will be ~9.81, but depends on device orientation)
            gravity_x = self.bias['x']
            gravity_y = self.bias['y']
            gravity_z = self.bias['z']
            self.gravity = math.sqrt(gravity_x**2 + gravity_y**2 + gravity_z**2)

            self.calibrated = True

            # Initialize acceleration calculator (handles device tilt correctly)
            if HAS_ACCEL_CALC:
                self.accel_calc = AccelerationCalculator(
                    gravity_magnitude=self.gravity,
                    bias_x=self.bias['x'],
                    bias_y=self.bias['y'],
                    bias_z=self.bias['z'],
                    method='magnitude'  # Use magnitude-based (orientation-independent)
                )

            if not silent:
                print(f"✓ Calibrated. Bias: x={self.bias['x']:.2f}, y={self.bias['y']:.2f}, z={self.bias['z']:.2f}, Gravity: {self.gravity:.2f} m/s²")

            # Validate calibration with health monitor
            if health_monitor:
                valid, gravity, issues = health_monitor.validate_calibration(
                    self.bias['x'], self.bias['y'], self.bias['z']
                )
                if not valid and not silent:
                    print("⚠ Calibration validation warnings:")
                    for issue in issues:
                        print(f"  ⚠ {issue}")
        else:
            if not silent:
                print("⚠ Calibration failed, using zero bias")

    def try_recalibrate(self, is_stationary):
        """Attempt dynamic re-calibration when device is stationary (handles rotation)"""
        current_time = time.time()

        if self.last_recal_time is None:
            self.last_recal_time = current_time

        # Check every recal_interval seconds if stationary
        if is_stationary and (current_time - self.last_recal_time >= self.recal_interval):
            # Collect samples while stationary
            if len(self.recal_buffer) >= 10:
                # Have enough samples, recalibrate silently
                old_bias_x = self.bias['x']
                old_gravity = self.gravity

                # Perform re-calibration with collected samples
                calibration_samples = self.recal_buffer[:10]
                self.bias['x'] = mean(s['x'] for s in calibration_samples)
                self.bias['y'] = mean(s['y'] for s in calibration_samples)
                self.bias['z'] = mean(s['z'] for s in calibration_samples)

                gravity_x = self.bias['x']
                gravity_y = self.bias['y']
                gravity_z = self.bias['z']
                new_gravity = math.sqrt(gravity_x**2 + gravity_y**2 + gravity_z**2)
                self.gravity = new_gravity

                # Only log if significant change detected (> 0.5 m/s² gravity drift)
                gravity_drift = abs(new_gravity - old_gravity)
                if gravity_drift > 0.5:
                    print(f"⚡ Dynamic recal: gravity {old_gravity:.2f} → {new_gravity:.2f} m/s² (drift: {gravity_drift:.2f})")

                self.recal_buffer = []
                self.last_recal_time = current_time

        # Add to buffer if stationary
        if is_stationary and len(self.recal_buffer) < 10:
            # We'll feed this from the main run loop
            pass
        elif not is_stationary:
            # Clear buffer when moving again
            self.recal_buffer = []
            self.last_recal_time = current_time

    def read_calibrated(self, raw):
        """Apply magnitude-based calibration (handles any device orientation)"""
        if raw:
            # Calculate magnitude of raw acceleration
            raw_magnitude = math.sqrt(raw['x']**2 + raw['y']**2 + raw['z']**2)

            # Subtract gravity magnitude to get true acceleration (motion only)
            # This works regardless of device tilt because we're using the magnitude
            motion_magnitude = raw_magnitude - self.gravity

            # Clamp to 0 if slightly negative (small noise)
            motion_magnitude = max(0, motion_magnitude)

            return {
                'x': raw['x'] - self.bias['x'],
                'y': raw['y'] - self.bias['y'],
                'z': raw['z'] - self.bias['z'],
                'magnitude': motion_magnitude,  # True motion magnitude (gravity removed)
                'timestamp': raw['timestamp']
            }
        return None

    def run(self):
        """Continuously read from daemon and apply calibration"""
        try:
            # Calibrate before starting
            if not self.calibrated:
                self.calibrate(health_monitor=self.health_monitor)

            while not self.stop_event.is_set():
                try:
                    # Read from daemon (non-blocking)
                    raw_data = self.sensor_daemon.get_data(timeout=0.1)

                    if raw_data:
                        # Track data quality with health monitor
                        if self.health_monitor:
                            quality, issues = self.health_monitor.check_data_quality(raw_data)
                            # Could log issues if needed: if issues: print(f"⚠ {issues}")

                        # Check for dynamic re-calibration opportunity
                        if self.fusion:
                            state = self.fusion.get_state()
                            is_stationary = state.get('is_stationary', False)

                            # Collect samples during stationary periods
                            if is_stationary and len(self.recal_buffer) < 10:
                                self.recal_buffer.append(raw_data)

                            # Attempt re-calibration
                            self.try_recalibrate(is_stationary)

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

                                                # FORCE recalibration on rotation detection
                                                # Use collected samples if available, otherwise recalibrate with current sample
                                                if len(self.recal_buffer) >= 5:
                                                    # Have enough samples collected - perform recalibration
                                                    old_bias_x = self.bias['x']
                                                    old_gravity = self.gravity

                                                    # Use available samples (up to 10)
                                                    cal_samples = self.recal_buffer[:10] if len(self.recal_buffer) >= 10 else self.recal_buffer
                                                    self.bias['x'] = mean(s['x'] for s in cal_samples)
                                                    self.bias['y'] = mean(s['y'] for s in cal_samples)
                                                    self.bias['z'] = mean(s['z'] for s in cal_samples)

                                                    gravity_x = self.bias['x']
                                                    gravity_y = self.bias['y']
                                                    gravity_z = self.bias['z']
                                                    new_gravity = math.sqrt(gravity_x**2 + gravity_y**2 + gravity_z**2)
                                                    self.gravity = new_gravity

                                                    gravity_drift = abs(new_gravity - old_gravity)
                                                    print(f"⚡ Dynamic recal: gravity {old_gravity:.2f} → {new_gravity:.2f} m/s² (drift: {gravity_drift:.2f})")

                                                    self.recal_buffer = []
                                                    self.last_recal_time = current_time_check
                                                else:
                                                    # Not enough samples yet, but rotation detected - prepare for next samples
                                                    print(f"   (Collecting calibration samples: {len(self.recal_buffer)}/10)")
                                                    self.last_recal_time = current_time_check

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
                            except:
                                # Queue full, skip sample
                                pass

                except Exception as e:
                    # Log error but continue running
                    if not self.stop_event.is_set():
                        print(f"\n⚠ Accel thread error (continuing): {e}")
                    time.sleep(0.1)

        except Exception as e:
            # Fatal error - thread will die
            print(f"\n⚠ Accel thread FATAL error: {e}")
            import traceback
            traceback.print_exc()


class BatteryReader:
    """Read battery status from Termux API"""

    @staticmethod
    def read():
        """Read current battery status"""
        try:
            result = subprocess.run(
                ['termux-battery-status'],
                capture_output=True,
                text=True,
                timeout=2
            )

            if result.returncode == 0 and result.stdout:
                data = json_loads(result.stdout)
                return {
                    'percentage': data.get('percentage'),
                    'current': data.get('current'),  # μA (negative = discharging)
                    'temperature': data.get('temperature'),  # °C
                    'voltage': data.get('voltage'),  # mV
                    'status': data.get('status'),  # CHARGING/DISCHARGING
                    'charge_counter': data.get('charge_counter')  # μAh remaining
                }
        except Exception:
            pass

        return None


class MotionTrackerV2:
    """Main motion tracking application - Multithreaded Edition"""

    def __init__(self, auto_save_interval=120, battery_sample_interval=10, accel_sample_rate=20, filter_type='ekf', enable_gyro=False):
        self.auto_save_interval = auto_save_interval  # seconds
        self.battery_sample_interval = battery_sample_interval  # sample battery every N seconds
        # accel_sample_rate: Hardware provides ~15-20 Hz (LSM6DSO sensor optimized with -d 50)
        # See SENSOR_POLLING_FINDINGS.md for detailed analysis
        self.accel_sample_rate = accel_sample_rate  # Hz
        self.filter_type = filter_type  # 'complementary', 'kalman', or 'ekf'
        self.enable_gyro = enable_gyro  # Enable gyroscope support in filters that support it

        # Sensor fusion (swappable filter implementations)
        # EKF supports enable_gyro; other filters don't accept this parameter
        if filter_type == 'ekf':
            self.fusion = get_filter(filter_type=filter_type, enable_gyro=enable_gyro)
        else:
            self.fusion = get_filter(filter_type=filter_type)
        self.battery = BatteryReader()

        # Accelerometer health monitoring
        self.health_monitor = None
        if HAS_HEALTH_MONITOR:
            self.health_monitor = AccelHealthMonitor(target_sample_rate=accel_sample_rate)

        # Threading
        self.stop_event = threading.Event()
        self.gps_queue = Queue(maxsize=100)
        self.accel_queue = Queue(maxsize=1000)  # Larger for high-frequency data

        # Sensor daemon (single long-lived process)
        self.sensor_daemon = None

        # Gyroscope daemon and rotation detector
        self.gyro_daemon = None
        self.rotation_detector = None

        # Threads
        self.gps_thread = None
        self.accel_thread = None

        # Data storage - use bounded deques to prevent memory runaway
        # Keep ~30 minutes of samples in memory (adjustable)
        self.max_memory_samples = 10000  # ~30 min at 5 GPS/min + 180k accel/hour = trim aggressively
        self.samples = deque(maxlen=self.max_memory_samples)  # GPS-based samples - BOUNDED
        self.accel_samples = deque(maxlen=self.max_memory_samples)  # Accelerometer samples - BOUNDED
        self.battery_samples = deque(maxlen=1000)  # Battery samples - BOUNDED
        self.battery_start = None

        # Tracking state
        self.start_time = None
        self.save_count = 0
        self.last_save_time = None
        self.shutdown_requested = False

        # Memory monitoring
        self.memory_threshold = 80  # Percent - warn/throttle at this level
        self.last_memory_check = time.time()

        # GPS failure tracking
        self.gps_failure_count = 0
        self.max_consecutive_failures = 10

        # Thread health monitoring
        self.last_thread_health_check = time.time()
        self.thread_health_check_interval = 5.0  # Check every 5 seconds
        self.thread_restart_count = {'gps': 0, 'accel': 0}
        self.max_thread_restarts = 3  # Max restart attempts per thread

        # Setup signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully"""
        signal_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
        print(f"\n\n⚠ Received {signal_name}, shutting down threads...")
        self.shutdown_requested = True
        self.stop_event.set()

    def check_memory(self):
        """Check system memory and return throttle level"""
        try:
            memory = psutil.virtual_memory()
            if memory.percent > 85:
                return 2  # Critical
            elif memory.percent > self.memory_threshold:
                return 1  # Warning
            return 0  # OK
        except:
            return 0

    def check_thread_health(self):
        """
        Check if critical threads are alive and healthy.
        Returns dict with thread status and recommendations.
        """
        status = {
            'gps_alive': self.gps_thread.is_alive() if self.gps_thread else False,
            'accel_alive': self.accel_thread.is_alive() if self.accel_thread else False,
            'healthy': True,
            'warnings': []
        }

        # Check GPS thread
        if self.gps_thread and not status['gps_alive']:
            status['healthy'] = False
            status['warnings'].append("GPS thread died unexpectedly")

        # Check accelerometer thread
        if self.accel_thread and not status['accel_alive']:
            status['healthy'] = False
            status['warnings'].append("Accelerometer thread died unexpectedly")

        return status

    def restart_accel_thread(self):
        """Attempt to restart the accelerometer thread after failure"""
        if self.thread_restart_count['accel'] >= self.max_thread_restarts:
            print(f"⚠ Accelerometer thread failed {self.max_thread_restarts} times, not restarting")
            return False

        print(f"\n⚡ Attempting to restart accelerometer thread (attempt {self.thread_restart_count['accel'] + 1}/{self.max_thread_restarts})...")

        try:
            # Stop existing thread if any
            if self.accel_thread:
                self.accel_thread.join(timeout=1)

            # Clear the queue to prevent stale data
            while not self.accel_queue.empty():
                try:
                    self.accel_queue.get_nowait()
                except:
                    break

            # Restart with pure Python version (safer fallback)
            self.accel_thread = AccelerometerThread(
                self.accel_queue,
                self.stop_event,
                self.sensor_daemon,
                fusion=self.fusion,
                sample_rate=self.accel_sample_rate
            )
            self.accel_thread.start()

            self.thread_restart_count['accel'] += 1
            print(f"✓ Accelerometer thread restarted successfully")
            return True

        except Exception as e:
            print(f"⚠ Failed to restart accelerometer thread: {e}")
            return False

    def restart_gps_thread(self):
        """Attempt to restart the GPS thread after failure"""
        if self.thread_restart_count['gps'] >= self.max_thread_restarts:
            print(f"⚠ GPS thread failed {self.max_thread_restarts} times, not restarting")
            return False

        print(f"\n⚡ Attempting to restart GPS thread (attempt {self.thread_restart_count['gps'] + 1}/{self.max_thread_restarts})...")

        try:
            # Stop existing thread if any
            if self.gps_thread:
                self.gps_thread.join(timeout=1)

            # Clear the queue
            while not self.gps_queue.empty():
                try:
                    self.gps_queue.get_nowait()
                except:
                    break

            # Restart GPS thread
            self.gps_thread = GPSThread(self.gps_queue, self.stop_event)
            self.gps_thread.start()

            self.thread_restart_count['gps'] += 1
            print(f"✓ GPS thread restarted successfully")
            return True

        except Exception as e:
            print(f"⚠ Failed to restart GPS thread: {e}")
            return False

    def start_threads(self):
        """Start background sensor threads"""
        print("Starting background sensor threads...")

        # Start persistent IMU daemon with PAIRED accelerometer + gyroscope
        # (they are from the same hardware sensor and MUST be initialized together)
        # Use 50ms delay: hardware maxes out at ~17.3Hz, so lower delays just add overhead
        # See SENSOR_POLLING_FINDINGS.md for detailed analysis
        delay_ms = 50
        self.sensor_daemon = PersistentAccelDaemon(delay_ms=delay_ms)
        if not self.sensor_daemon.start():
            print("⚠ Accelerometer daemon failed to start, continuing anyway...")
        else:
            # Give daemon a moment to start producing data before validation
            time.sleep(1.5)

            # Validate daemon is producing data
            if self.health_monitor:
                success, sample_count, rate = self.health_monitor.validate_startup(
                    self.sensor_daemon,
                    duration=5,
                    target_samples=int(self.accel_sample_rate * 5)
                )
                if not success:
                    print("\n⚠ WARNING: Accelerometer startup validation FAILED")
                    print("  Data quality may be compromised. Check sensor connection.")
                    print("  Continuing with caution...\n")
            else:
                # If health monitor not available, give daemon time to produce samples
                # before calibration (normally done by validate_startup's 5-second test)
                print("  Giving daemon 2 seconds to start producing samples...")
                time.sleep(2)

        # Start gyroscope daemon (shared with accelerometer - same IMU hardware)
        print("Starting gyroscope daemon...")
        try:
            # Pass accel_daemon so gyro uses the shared sensor stream
            self.gyro_daemon = PersistentGyroDaemon(accel_daemon=self.sensor_daemon, delay_ms=50)
            if self.gyro_daemon.start():
                print("✓ Gyroscope daemon started (using shared IMU stream)")
                # Give daemon a moment to start producing data
                time.sleep(0.5)

                # If EKF filter is enabled, recreate it with gyro support
                if self.filter_type == 'ekf' and self.enable_gyro:
                    print("  → Enabling gyro support in EKF filter...")
                    self.fusion = get_filter(filter_type='ekf', enable_gyro=True)
                    print("  ✓ EKF filter now has gyroscope orientation tracking enabled")
            else:
                print("⚠ Gyroscope daemon failed to start (rotation detection disabled)")
                self.gyro_daemon = None
        except Exception as e:
            print(f"⚠ Failed to initialize gyroscope daemon: {e}")
            self.gyro_daemon = None

        # Start GPS thread
        self.gps_thread = GPSThread(self.gps_queue, self.stop_event)
        self.gps_thread.start()
        print("✓ GPS thread started")

        # Start accelerometer thread with better error handling
        try:
            if HAS_CYTHON and self.sensor_daemon:
                # ✓ CYTHON VERSION - Better performance, less sample loss, 70% less CPU
                # First, do calibration using pure Python thread
                temp_accel_thread = AccelerometerThread(
                    self.accel_queue,
                    self.stop_event,
                    self.sensor_daemon,
                    fusion=self.fusion,
                    sample_rate=self.accel_sample_rate
                )
                temp_accel_thread.calibrate(health_monitor=self.health_monitor)

                # Now use Cython processor with calibration bias and gravity
                self.accel_processor = FastAccelProcessor(
                    self.sensor_daemon,
                    self.accel_queue,
                    temp_accel_thread.bias,
                    temp_accel_thread.gravity,
                    self.stop_event
                )

                # Start Cython processor in a thread
                self.accel_thread = threading.Thread(
                    target=self.accel_processor.run,
                    daemon=True
                )
                self.accel_thread.start()
                print(f"✓ Accelerometer thread started ({self.accel_sample_rate} Hz) [CYTHON OPTIMIZED - 70% less CPU]")

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
                print(f"✓ Accelerometer thread started ({self.accel_sample_rate} Hz)")

        except Exception as e:
            print(f"⚠ Failed to start accelerometer thread: {e}")
            print("⚠ Continuing with GPS-only tracking")
            self.accel_thread = None

    def track(self, duration_minutes=None):
        """Main tracking loop"""
        self.start_time = datetime.now()

        print("\n" + "="*80)
        print("GPS + ACCELEROMETER MOTION TRACKER V2 - Multithreaded Edition")
        print("High-Frequency Sensor Fusion")
        print("="*80)
        print(f"Start time: {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}")

        if duration_minutes:
            print(f"Running for {duration_minutes} minutes (Ctrl+C to stop early)")
        else:
            print("Running in CONTINUOUS mode (Ctrl+C to stop)")

        # Acquire wakelock
        try:
            result = subprocess.run(['termux-wake-lock'], check=False, capture_output=True)
            print("✓ Wakelock acquired")
        except:
            print("⚠ Could not acquire wakelock")

        # Get initial battery
        self.battery_start = self.battery.read()

        # Start background threads
        self.start_threads()

        print("\nWaiting for GPS fix...", flush=True)

        # Wait for first GPS fix
        gps_locked = False
        while not gps_locked and not self.shutdown_requested:
            try:
                gps_data = self.gps_queue.get(timeout=1)
                if gps_data and gps_data.get('latitude'):
                    print(f"✓ GPS locked: {gps_data['latitude']:.6f}, {gps_data['longitude']:.6f}\n")
                    # Process this first GPS sample
                    self.fusion.update_gps(
                        gps_data['latitude'],
                        gps_data['longitude'],
                        gps_data.get('speed'),
                        gps_data.get('accuracy')
                    )
                    gps_locked = True
            except Empty:
                pass

        if not gps_locked:
            print("Failed to get GPS lock. Exiting.")
            self.stop_event.set()
            return

        print("Tracking... (Press Ctrl+C to stop)\n")
        print(f"Auto-save enabled: every {self.auto_save_interval//60} minutes")
        print(f"Accelerometer sampling: {self.accel_sample_rate} Hz")
        print(f"{'Time':<8} | {'Speed (km/h)':<12} | {'Distance (m)':<12} | {'Accel':<10} | {'GPS Acc':<8}")
        print("-" * 90)

        self.last_save_time = time.time()
        last_battery_time = time.time()
        last_display_time = time.time()
        last_health_check_time = time.time()
        gps_sample_count = 0
        accel_sample_count = 0

        try:
            while not self.shutdown_requested:
                current_time = time.time()
                elapsed = (datetime.now() - self.start_time).total_seconds()

                # Check duration
                if duration_minutes and elapsed > duration_minutes * 60:
                    break

                # Check memory
                memory_throttle = self.check_memory()
                if memory_throttle > 0 and current_time - self.last_memory_check >= 5.0:
                    memory = psutil.virtual_memory()
                    throttle_str = "CRITICAL" if memory_throttle == 2 else "WARNING"
                    print(f"\n⚠ Memory {throttle_str}: {memory.percent:.1f}% ({memory.used/1024/1024:.0f}MB / {memory.total/1024/1024:.0f}MB)")
                    self.last_memory_check = current_time

                # THREAD HEALTH CHECK
                if current_time - self.last_thread_health_check >= self.thread_health_check_interval:
                    health_status = self.check_thread_health()

                    if not health_status['healthy']:
                        for warning in health_status['warnings']:
                            print(f"\n⚠ THREAD HEALTH: {warning}")

                        # Attempt to restart failed threads
                        if not health_status['accel_alive'] and self.accel_thread:
                            self.restart_accel_thread()

                        if not health_status['gps_alive'] and self.gps_thread:
                            if not self.restart_gps_thread():
                                print("⚠ GPS thread failed permanently, continuing with accelerometer-only")

                    self.last_thread_health_check = current_time

                # ACCELEROMETER HEALTH CHECK
                if self.health_monitor and current_time - last_health_check_time >= 30.0:
                    # Check every 30 seconds
                    diag = self.health_monitor.get_diagnostics()

                    # Alert on critical issues
                    if diag['queue_stalled']:
                        print(f"\n⚠ ACCEL HEALTH: Queue stalled for {diag['time_since_last_sample_ms']:.0f}ms")
                    elif not diag['current_sample_rate_healthy']:
                        print(f"\n⚠ ACCEL HEALTH: Sample rate {diag['current_sample_rate_hz']:.1f}Hz out of range")
                    elif diag['gravity_drift_detected']:
                        print(f"\n⚠ ACCEL HEALTH: Gravity drift detected, recalibration may be needed")

                    last_health_check_time = current_time

                # AUTO-SAVE check
                if current_time - self.last_save_time >= self.auto_save_interval:
                    print(f"\n⏰ Auto-saving data (save #{self.save_count + 1})...")
                    try:
                        self.save_data(auto_save=True, clear_after_save=True)
                        self.last_save_time = current_time
                        self.save_count += 1
                        print(f"✓ Auto-save complete (GPS: {len(self.samples)}, Accel: {len(self.accel_samples)} samples)\n")
                    except Exception as e:
                        print(f"⚠ Auto-save failed: {e}, continuing...\n")

                # Process GPS queue (non-blocking)
                try:
                    while True:
                        gps_data = self.gps_queue.get_nowait()

                        if gps_data and gps_data.get('latitude'):
                            velocity, distance = self.fusion.update_gps(
                                gps_data['latitude'],
                                gps_data['longitude'],
                                gps_data.get('speed'),
                                gps_data.get('accuracy')
                            )

                            # Sample battery periodically
                            battery_data = None
                            if current_time - last_battery_time >= self.battery_sample_interval:
                                battery_data = self.battery.read()
                                if battery_data:
                                    self.battery_samples.append({
                                        'timestamp': datetime.now().isoformat(),
                                        'elapsed': elapsed,
                                        'battery': battery_data
                                    })
                                last_battery_time = current_time

                            # Log GPS sample
                            self.samples.append({
                                'timestamp': datetime.now().isoformat(),
                                'elapsed': elapsed,
                                'velocity': velocity,
                                'distance': distance,
                                'gps': gps_data,
                                'battery': battery_data
                            })

                            gps_sample_count += 1
                            self.gps_failure_count = 0

                            # Display update (throttled to once per second)
                            if current_time - last_display_time >= 1.0:
                                speed_kmh = velocity * 3.6
                                time_str = f"{int(elapsed//60)}:{int(elapsed%60):02d}"
                                accuracy = gps_data.get('accuracy', 0)

                                # Get recent accelerometer magnitude
                                state = self.fusion.get_state()
                                accel_mag = state.get('accel_velocity', 0)

                                print(f"{time_str:<8} | {speed_kmh:>10.2f} | {distance:>10.1f} | {accel_mag:>8.2f} | {accuracy:>6.1f}m")
                                last_display_time = current_time

                except Empty:
                    pass

                # Process accelerometer queue (non-blocking, drain multiple samples)
                accel_batch_count = 0
                try:
                    while accel_batch_count < 50:  # Process up to 50 samples per loop
                        accel_data = self.accel_queue.get_nowait()

                        if accel_data:
                            # Use calibrated magnitude-based acceleration (handles device tilt)
                            # This is the 'magnitude' field already calculated by AccelerometerThread
                            motion_accel = accel_data.get('magnitude', 0)

                            # Update fusion with accelerometer
                            velocity, distance = self.fusion.update_accelerometer(motion_accel)

                            # Log accelerometer sample (full detail)
                            self.accel_samples.append({
                                'timestamp': accel_data['timestamp'],
                                'elapsed': accel_data['timestamp'] - self.start_time.timestamp(),
                                'x': accel_data['x'],
                                'y': accel_data['y'],
                                'z': accel_data['z'],
                                'magnitude': accel_data['magnitude'],
                                'velocity_estimate': velocity
                            })

                            accel_sample_count += 1
                            accel_batch_count += 1

                except Empty:
                    pass

                # Brief sleep to prevent CPU spinning
                time.sleep(0.01)

        except KeyboardInterrupt:
            print("\n\nStopped by user.")
        except Exception as e:
            print(f"\n\n⚠ ERROR: {e}")
            print("Auto-saving data before exit...")
            import traceback
            traceback.print_exc()

        # Stop threads
        print("\nStopping background threads...")
        self.stop_event.set()

        # Wait for threads with timeout
        threads_to_stop = []
        if self.gps_thread and self.gps_thread.is_alive():
            threads_to_stop.append(('GPS', self.gps_thread))
        if self.accel_thread and self.accel_thread.is_alive():
            threads_to_stop.append(('Accel', self.accel_thread))

        for name, thread in threads_to_stop:
            try:
                thread.join(timeout=2)
                if thread.is_alive():
                    print(f"⚠ {name} thread did not exit cleanly (still running)")
                else:
                    print(f"  ✓ {name} thread stopped")
            except Exception as e:
                print(f"⚠ Error stopping {name} thread: {e}")

        # Stop sensor daemon
        if self.sensor_daemon:
            try:
                self.sensor_daemon.stop()
                print("  ✓ Accelerometer daemon stopped")
            except Exception as e:
                print(f"⚠ Error stopping accelerometer daemon: {e}")

        # Stop gyroscope daemon
        if self.gyro_daemon:
            try:
                self.gyro_daemon.stop()
                print("  ✓ Gyroscope daemon stopped")
            except Exception as e:
                print(f"⚠ Error stopping gyroscope daemon: {e}")

        # Kill any lingering termux-sensor and stdbuf processes
        # This is a safety net in case threads didn't exit cleanly
        try:
            subprocess.run(['pkill', '-9', 'termux-sensor'], check=False,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['pkill', '-9', 'stdbuf'], check=False,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except:
            pass

        print("✓ Threads stopped")

        # Release wakelock
        try:
            subprocess.run(['termux-wake-unlock'], check=False)
            print("✓ Wakelock released")
        except:
            pass

        # Final save
        print("\nSaving final data...")
        self.print_summary()
        self.save_data(auto_save=False)

        # Print final accelerometer diagnostics
        if self.health_monitor:
            print("\nFinal Accelerometer Diagnostics:")
            self.health_monitor.print_diagnostics()

    def print_summary(self):
        """Print summary statistics"""
        if not self.samples:
            print("No GPS data collected")
            return

        print("\n" + "="*80)
        print("TRACKING SESSION SUMMARY")
        print("="*80)

        duration = (datetime.now() - self.start_time).total_seconds()

        # Handle deques - convert to list temporarily
        samples_list = list(self.samples)
        accel_samples_list = list(self.accel_samples)
        battery_samples_list = list(self.battery_samples)

        if not samples_list:
            print("No GPS data collected")
            return

        final_distance = samples_list[-1]['distance']

        print(f"\nSession duration:     {int(duration//60)}m {int(duration%60)}s")
        print(f"Total distance:       {final_distance/1000:.2f} km ({final_distance:.0f} m)")
        print(f"GPS samples (in memory): {len(samples_list)}")
        print(f"Accelerometer samples (in memory): {len(accel_samples_list)}")

        if samples_list:
            velocities = [s['velocity'] * 3.6 for s in samples_list]
            print(f"Average speed:        {sum(velocities)/len(velocities):.1f} km/h")
            print(f"Max speed:            {max(velocities):.1f} km/h")

        # Battery stats
        if self.battery_start and battery_samples_list:
            battery_end = battery_samples_list[-1]['battery']
            print(f"\nBattery:")
            print(f"  Start: {self.battery_start['percentage']}%")
            print(f"  End:   {battery_end['percentage']}%")
            print(f"  Drop:  {self.battery_start['percentage'] - battery_end['percentage']}%")

        print(f"\nAuto-saves performed: {self.save_count}")
        print("="*80)

    def save_data(self, auto_save=False, clear_after_save=False):
        """Save tracking data to files"""
        timestamp = self.start_time.strftime('%Y%m%d_%H%M%S')
        base_filename = os.path.join(SESSIONS_DIR, f"motion_track_v2_{timestamp}")

        # Convert deques to lists for JSON serialization
        samples_list = list(self.samples)
        accel_samples_list = list(self.accel_samples)
        battery_samples_list = list(self.battery_samples)

        # Prepare data
        data = {
            'version': 2,
            'start_time': self.start_time.isoformat(),
            'end_time': datetime.now().isoformat(),
            'total_distance': samples_list[-1]['distance'] if samples_list else 0,
            'gps_samples': samples_list,
            'accel_samples': accel_samples_list,
            'battery_samples': battery_samples_list,
            'battery_start': self.battery_start,
            'auto_save_count': self.save_count,
            'config': {
                'accel_sample_rate': self.accel_sample_rate,
                'auto_save_interval': self.auto_save_interval,
                'filter_type': self.filter_type
            }
        }

        if auto_save:
            # Compressed auto-save
            filename = f"{base_filename}.json.gz"
            temp_filename = f"{filename}.tmp"

            with gzip.open(temp_filename, 'wt', encoding='utf-8') as f:
                json_dump(data, f, separators=(',', ':'))

            # Atomic rename
            os.rename(temp_filename, filename)

            # Clear samples after saving to free memory
            if clear_after_save:
                self.samples.clear()
                self.accel_samples.clear()
                # Keep battery samples for final report

                # CRITICAL FIX: Restart accelerometer thread after deque clear
                # Without restart, thread's internal state becomes stale and stops producing data
                print(f"  ↻ Restarting accelerometer thread to resync after deque clear...", file=sys.stderr)
                if self.restart_accel_thread():
                    print(f"  ✓ Accelerometer thread restarted successfully", file=sys.stderr)
                else:
                    print(f"  ⚠ Accelerometer thread restart failed (will attempt retry on next auto-save)", file=sys.stderr)
        else:
            # Final save - both compressed and uncompressed
            # Uncompressed JSON
            filename_json = f"{base_filename}.json"
            temp_filename = f"{filename_json}.tmp"

            with open(temp_filename, 'w') as f:
                json_dump(data, f, indent=2)

            os.rename(temp_filename, filename_json)

            # Compressed
            filename_gz = f"{base_filename}.json.gz"
            with gzip.open(filename_gz, 'wt', encoding='utf-8') as f:
                json_dump(data, f, separators=(',', ':'))

            # GPX export (GPS samples only)
            self.export_gpx(timestamp)

            print(f"\n✓ Data saved:")
            print(f"  {filename_json}")
            print(f"  {filename_gz}")
            print(f"  motion_track_v2_{timestamp}.gpx")

    def export_gpx(self, timestamp):
        """Export GPS track to GPX format"""
        filename = os.path.join(SESSIONS_DIR, f"motion_track_v2_{timestamp}.gpx")

        # Convert deque to list for iteration
        samples_list = list(self.samples)

        gpx_content = f'''<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="MotionTrackerV2">
  <metadata>
    <name>Motion Track {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}</name>
    <time>{self.start_time.isoformat()}Z</time>
  </metadata>
  <trk>
    <name>Motion Track</name>
    <trkseg>
'''

        for sample in samples_list:
            if sample.get('gps') and sample['gps'].get('latitude'):
                lat = sample['gps']['latitude']
                lon = sample['gps']['longitude']
                alt = sample['gps'].get('altitude', 0)
                timestamp_iso = sample['timestamp']

                gpx_content += f'      <trkpt lat="{lat}" lon="{lon}">\n'
                gpx_content += f'        <ele>{alt}</ele>\n'
                gpx_content += f'        <time>{timestamp_iso}Z</time>\n'
                gpx_content += f'      </trkpt>\n'

        gpx_content += '''    </trkseg>
  </trk>
</gpx>
'''

        with open(filename, 'w') as f:
            f.write(gpx_content)


def main():
    import sys

    print("\n" + "="*80)
    print("GPS + ACCELEROMETER MOTION TRACKER V2")
    print("Multithreaded High-Frequency Data Capture")
    print("="*80)

    try:
        # Parse command line arguments
        duration = None
        accel_rate = 20  # Default 20 Hz (hardware provides: LSM6DSO accelerometer ~15-20Hz with -d 50)
        filter_type = 'ekf'  # Default filter: EKF (optimal for GPS+accel+gyro)
        enable_gyro = False  # Enable gyroscope in EKF if available

        for arg in sys.argv[1:]:
            if arg == "--test":
                duration = 2  # 2 minutes for testing
            elif arg.startswith("--filter="):
                filter_type = arg.split("=")[1].lower()
            elif arg == "--enable-gyro":
                enable_gyro = True
            elif arg == "--gyro":
                enable_gyro = True
            elif arg.isdigit():
                duration = int(arg)
            else:
                try:
                    accel_rate = int(arg)
                except:
                    pass

        # Validate filter type
        if filter_type not in ['complementary', 'kalman', 'ekf']:
            print(f"⚠ Unknown filter type: {filter_type}")
            print(f"   Use: --filter=complementary, --filter=kalman, or --filter=ekf")
            print(f"   Defaulting to: ekf")
            filter_type = 'ekf'

        print(f"\nConfiguration:")
        if duration:
            print(f"  Duration: {duration} minutes")
        else:
            print(f"  Duration: Continuous (Ctrl+C to stop)")
        print(f"  Accelerometer: {accel_rate} Hz")
        print(f"  Sensor Fusion: {filter_type.upper()} filter")
        if enable_gyro and filter_type == 'ekf':
            print(f"  Gyroscope: ENABLED (quaternion orientation tracking)")
        elif filter_type == 'ekf':
            print(f"  Gyroscope: Available if sensor detected (use --enable-gyro)")
        print(f"  Auto-save: Every 2 minutes")
        print("\nStarting in 3 seconds...")
        time.sleep(3)

        tracker = MotionTrackerV2(accel_sample_rate=accel_rate, filter_type=filter_type, enable_gyro=enable_gyro)
        tracker.track(duration_minutes=duration)

    except KeyboardInterrupt:
        print("\n\nExiting...")
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
