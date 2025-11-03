#!/usr/bin/env python3
"""
Real-Time Filter Comparison Test - EKF vs Complementary

Runs both filters in parallel on live sensor data and displays metrics side-by-side.
Perfect for evaluating EKF performance against the baseline Complementary filter.

⚠️  MANDATORY: ALWAYS RUN VIA SHELL SCRIPT, NOT DIRECTLY
================================================================================
WRONG:  python test_ekf_vs_complementary.py 5
RIGHT:  ./test_ekf.sh 5

The shell script (./test_ekf.sh) is REQUIRED because it:
  1. Cleans up stale sensor processes before startup
  2. Validates accelerometer is accessible (retry logic)
  3. Ensures proper sensor initialization
  4. Handles signal cleanup on exit

Running this directly will fail with "No accelerometer data" errors.
================================================================================

Usage (via shell script - the only correct way):
    ./test_ekf.sh 5          # Run for 5 minutes
    ./test_ekf.sh 10 --gyro  # 10 minutes with gyroscope
"""

import subprocess
import threading
import time
import sys
import json
import os
import gzip
import psutil
from queue import Queue, Empty
from datetime import datetime
from collections import deque

# Try orjson for speed
try:
    import orjson
    HAS_ORJSON = True
except ImportError:
    HAS_ORJSON = False
    import json

from filters import get_filter
from motion_tracker_v2 import PersistentAccelDaemon, PersistentGyroDaemon
from metrics_collector import MetricsCollector

# Session directory for organized data storage (matches motion_tracker_v2.py pattern)
SESSIONS_DIR = os.path.join(os.path.dirname(__file__), "..", "motion_tracker_sessions")
if not os.path.exists(SESSIONS_DIR):
    os.makedirs(SESSIONS_DIR, exist_ok=True)


class PersistentSensorDaemon:
    """Read from persistent termux-sensor stream"""

    def __init__(self, sensor_type='ACCELEROMETER', delay_ms=50):
        self.sensor_type = sensor_type
        self.delay_ms = delay_ms
        self.data_queue = Queue(maxsize=1000)
        self.process = None
        self.stop_event = threading.Event()

    def start(self):
        try:
            # Use stdbuf with line buffering for consistent output streaming
            # This ensures data is flushed line-by-line for reliable Python iteration
            # NOTE: Gyroscope support is limited on Termux - sensor HAL may not stream
            # data to Python subprocesses even though direct termux-sensor calls work
            cmd = ['stdbuf', '-oL', 'termux-sensor', '-s', self.sensor_type, '-d', str(self.delay_ms)]

            # Use text=True for consistent line-by-line iteration and automatic UTF-8 decoding
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )

            reader = threading.Thread(target=self._read_loop, daemon=True)
            reader.start()
            return True
        except Exception as e:
            print(f"Failed to start {self.sensor_type} daemon: {e}")
            return False

    def _read_loop(self):
        import sys
        packets_received = 0
        last_packet_time = time.time()
        watchdog_timeout = 5.0  # 5 seconds - if no data, restart daemon

        try:
            json_buffer = ""
            brace_depth = 0

            for line in self.process.stdout:
                # WATCHDOG: Detect data stall (subprocess hung but not dead)
                if time.time() - last_packet_time > watchdog_timeout:
                    print(f"[{self.sensor_type}] ⚠ WATCHDOG: No data for {watchdog_timeout}s, daemon stalled", file=sys.stderr)
                    break  # Exit loop so daemon can be restarted

                # INTERRUPTIBLE: Check for graceful shutdown
                if self.stop_event.is_set():
                    print(f"[{self.sensor_type}] Stop event received, exiting loop", file=sys.stderr)
                    break

                # line is already a string (text=True mode)
                json_buffer += line
                brace_depth += line.count('{') - line.count('}')

                if brace_depth == 0 and json_buffer.strip():
                    packets_received += 1
                    last_packet_time = time.time()  # Reset watchdog on valid packet

                    try:
                        data = json.loads(json_buffer)

                        # termux-sensor returns nested structure:
                        # {"sensor_name": {"values": [x, y, z]}}
                        # Extract and flatten for easier consumption
                        for sensor_key, sensor_data in data.items():
                            if isinstance(sensor_data, dict) and 'values' in sensor_data:
                                values = sensor_data['values']
                                # values is an array [x, y, z]
                                if isinstance(values, list) and len(values) >= 3:
                                    try:
                                        self.data_queue.put({
                                            'x': values[0],
                                            'y': values[1],
                                            'z': values[2],
                                            'timestamp': time.time()
                                        }, block=False)
                                        if packets_received <= 3:
                                            print(f"[{self.sensor_type}] Queued packet {packets_received}", file=sys.stderr)
                                    except Exception as q_err:
                                        # Queue full or other queue error - skip this packet but continue
                                        if packets_received <= 3:
                                            print(f"[{self.sensor_type}] Queue error on packet {packets_received}: {str(q_err)[:50]}", file=sys.stderr)
                                    break  # Only process first sensor
                        if packets_received <= 3 and not any(isinstance(v, dict) and 'values' in v for v in data.values()):
                            print(f"[{self.sensor_type}] Packet {packets_received} had no valid sensor data: {list(data.keys())}", file=sys.stderr)
                    except Exception as e:
                        if packets_received <= 3:
                            print(f"[{self.sensor_type}] Parse error on packet {packets_received}: {str(e)[:50]}", file=sys.stderr)
                    json_buffer = ""
                    brace_depth = 0
        except Exception as e:
            import sys
            print(f"[{self.sensor_type}] _read_loop exception: {e}", file=sys.stderr)
        finally:
            # CLEANUP: Always terminate subprocess (prevents zombies)
            if self.process:
                try:
                    self.process.terminate()
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        self.process.kill()
                    except:
                        pass
                except:
                    pass
            print(f"[{self.sensor_type}] Daemon loop exited (packets={packets_received})", file=sys.stderr)

    def get_data(self, timeout=0.1):
        try:
            return self.data_queue.get(timeout=timeout)
        except Empty:
            return None

    def stop(self):
        self.stop_event.set()
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except:
                pass


class PersistentGPSDaemon:
    """
    Persistent GPS daemon - continuously polls termux-location and queues results

    Key insight: Instead of blocking on subprocess.run() (3.7 second latency),
    we run termux-location in a background loop and read results from a queue.

    This achieves ~0.5-1 Hz GPS vs ~0.3 Hz with blocking calls.
    """

    def __init__(self):
        self.data_queue = Queue(maxsize=100)
        self.reader_thread = None
        self.stop_event = threading.Event()
        self.gps_process = None

    def start(self):
        """Start GPS daemon that continuously polls termux-location"""
        try:
            # Note: termux-location -r updates doesn't work as true continuous stream on this device
            # Instead, we poll in a loop. Even though each call has ~3.7s overhead due to DalvikVM init,
            # keeping ONE persistent process is better than spawning new processes repeatedly.
            #
            # CRITICAL FIX for "Connection refused" errors:
            # - Use aggressive exponential backoff (5→10→15→20→30s) on connection failures
            # - "Connection refused" = socket exhaustion in Termux:API backend
            # - Too frequent polling overwhelms the backend during long runs (30+ minutes)
            # - Solution: Significantly increase sleep time between polls to ~10s baseline
            #
            # Why this is better than one-shot calls:
            # - One-shot: new subprocess each time → new DalvikVM → 3.7s per call
            # - Polling: one subprocess with repeated calls → DalvikVM reused → ~0.5s per call + padding
            wrapper_script = '''
import subprocess
import json
import sys
import time

consecutive_failures = 0
max_failures = 5
failure_backoff_stages = [5, 10, 15, 20, 30]  # Seconds to wait per failure stage
current_backoff_stage = 0

while True:
    try:
        result = subprocess.run(
            ['termux-location', '-p', 'gps'],
            capture_output=True,
            text=True,
            timeout=8  # Increased from 5s → 8s to allow more processing time
        )
        if result.returncode == 0:
            print(result.stdout, flush=True)
            consecutive_failures = 0  # Reset on success
            current_backoff_stage = 0
        else:
            consecutive_failures += 1
            if consecutive_failures >= max_failures:
                # Move to next backoff stage, capped at max
                current_backoff_stage = min(current_backoff_stage + 1, len(failure_backoff_stages) - 1)
                backoff = failure_backoff_stages[current_backoff_stage]
                sys.stderr.write(f"GPS API failed {max_failures} times, backoff={backoff}s (stage {current_backoff_stage + 1})\\n")
                time.sleep(backoff)
                consecutive_failures = 0
    except subprocess.TimeoutExpired:
        sys.stderr.write("GPS timeout (8s exceeded)\\n")
        consecutive_failures += 1
        current_backoff_stage = min(current_backoff_stage + 1, len(failure_backoff_stages) - 1)
        time.sleep(failure_backoff_stages[current_backoff_stage])
    except Exception as e:
        sys.stderr.write(f"GPS error: {e}\\n")
        consecutive_failures += 1
        current_backoff_stage = min(current_backoff_stage + 1, len(failure_backoff_stages) - 1)
        time.sleep(failure_backoff_stages[current_backoff_stage])

    # Baseline sleep: 2s between polls for responsive GPS tracking
    # On consecutive failures, back off to avoid hammering the API
    baseline_sleep = 2.0 if consecutive_failures == 0 else 1.0
    time.sleep(baseline_sleep)
'''

            self.gps_process = subprocess.Popen(
                ['python3', '-c', wrapper_script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1
            )

            reader = threading.Thread(target=self._read_loop, daemon=True)
            reader.start()
            return True
        except Exception as e:
            print(f"Failed to start GPS daemon: {e}")
            return False

    def _read_loop(self):
        """Read GPS JSON objects from continuous stream (handles pretty-printed JSON)"""
        try:
            json_buffer = ""
            brace_depth = 0

            for line in self.gps_process.stdout:
                if self.stop_event.is_set():
                    break

                json_buffer += line
                brace_depth += line.count('{') - line.count('}')

                # Complete JSON object when braces balance
                if brace_depth == 0 and json_buffer.strip():
                    try:
                        data = json.loads(json_buffer)
                        gps_data = {
                            'latitude': float(data.get('latitude')),
                            'longitude': float(data.get('longitude')),
                            'accuracy': float(data.get('accuracy', 5.0)),
                            'altitude': float(data.get('altitude', 0)),
                            'bearing': float(data.get('bearing', 0)),
                            'speed': float(data.get('speed', 0))
                        }
                        try:
                            self.data_queue.put_nowait(gps_data)
                        except:
                            pass  # Queue full, drop oldest
                        json_buffer = ""
                    except Exception as e:
                        # Log malformed JSON but continue (don't crash)
                        pass  # Invalid JSON, skip
        except Exception as e:
            # Log any errors in the read loop (but don't crash - subprocess will exit)
            print(f"⚠️  [GPSDaemon] Reader thread error: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc(file=sys.stderr)

    def get_data(self, timeout=0.1):
        """Non-blocking read from GPS queue"""
        try:
            return self.data_queue.get(timeout=timeout)
        except Empty:
            return None

    def is_alive(self):
        """Check if GPS daemon subprocess is still running"""
        if not self.gps_process:
            return False
        poll_result = self.gps_process.poll()
        # poll() returns None if process is still running, non-None if it has exited
        return poll_result is None

    def get_status(self):
        """Get daemon status for debugging"""
        if not self.gps_process:
            return "NOT_STARTED"
        if not self.is_alive():
            exit_code = self.gps_process.poll()
            return f"DEAD (exit_code={exit_code})"
        return "ALIVE"

    def stop(self):
        """Stop GPS daemon"""
        self.stop_event.set()
        if self.gps_process:
            try:
                self.gps_process.terminate()
                self.gps_process.wait(timeout=2)
            except:
                pass


def parse_gps():
    """Legacy function - deprecated, use PersistentGPSDaemon instead"""
    try:
        result = subprocess.run(
            ['termux-location'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return {
                'latitude': float(data.get('latitude')),
                'longitude': float(data.get('longitude')),
                'accuracy': float(data.get('accuracy', 5.0)),
                'altitude': float(data.get('altitude', 0)),
                'bearing': float(data.get('bearing', 0)),
                'speed': float(data.get('speed', 0))
            }
    except:
        pass
    return None


class FilterComparison:
    """Run two filters in parallel and compare"""

    def __init__(self, duration_minutes=5, enable_gyro=False):
        self.duration_minutes = duration_minutes
        self.enable_gyro = enable_gyro
        self.stop_event = threading.Event()

        # Filters
        self.ekf = get_filter('ekf', enable_gyro=enable_gyro)
        self.complementary = get_filter('complementary')

        # Sensors (accelerometer and gyroscope are paired from same IMU hardware)
        self.accel_daemon = PersistentAccelDaemon(delay_ms=50)  # Stable baseline - hardware limited to ~15Hz
        self.gps_daemon = PersistentGPSDaemon()  # Continuous GPS polling daemon
        self.gyro_daemon = None  # Will be initialized if enable_gyro=True

        # Data storage - OPTIMIZED for memory efficiency
        # All data still saved to disk via auto-save, this is just in-memory history
        # GPS: 2,000 fixes @ 1 Hz = ~33 minutes (sufficient for single drive)
        # Accel: 30,000 samples @ 20 Hz actual = 25 minutes (increased buffer for auto-save delays)
        # Gyro: 30,000 samples @ 20 Hz actual = 25 minutes (paired with accel)
        # Comparison: 500 summaries (last 5 seconds at ~100Hz comparison rate)
        # Note: Hardware delivers ~20Hz not theoretical 50Hz, so buffer sized accordingly
        self.gps_samples = deque(maxlen=2000)
        self.accel_samples = deque(maxlen=30000)
        self.gyro_samples = deque(maxlen=30000)
        self.comparison_samples = deque(maxlen=500)

        # FIX 2: Thread lock for accumulated_data and deque operations
        self._save_lock = threading.Lock()

        # Metrics
        self.last_gps_time = None
        self.start_time = time.time()
        self.last_status_time = time.time()
        self.last_auto_save_time = time.time()

        # Memory monitoring
        self.process = psutil.Process()
        self.peak_memory = 0

        # Auto-save configuration
        self.auto_save_interval = 120  # Save every 2 minutes

        # Metrics collector (for gyro-EKF validation)
        self.metrics = None
        if enable_gyro:
            self.metrics = MetricsCollector(max_history=600)

        # Daemon restart tracking
        self.restart_counts = {
            'accel': 0,
            'gps': 0
        }
        self.max_restart_attempts = 3
        self.restart_cooldown = 10  # INCREASED from 5s → 10s (termux-sensor needs full resource release)

        # HEALTH MONITORING: Detect sensor silence and auto-restart
        self.last_accel_sample_time = time.time()
        self.last_gps_sample_time = time.time()
        self.accel_silence_threshold = 5.0  # Restart if no accel for 5 seconds
        self.gps_silence_threshold = 30.0   # Restart if no GPS for 30 seconds
        self.health_check_interval = 2.0    # Check health every 2 seconds

        # Gravity calibration - CRITICAL for complementary filter
        # Must subtract gravity magnitude from raw acceleration to detect true motion
        self.gravity = 9.81  # Default value, will be calibrated from first samples
        self.calibration_samples = []
        self.calibration_complete = False

        # FIX 6: Total GPS counter (cumulative across auto-saves)
        self.total_gps_fixes = 0

    def _calibrate_gravity(self):
        """Collect initial stationary samples to calibrate gravity magnitude"""
        print(f"\n✓ Calibrating accelerometer (collecting 20 stationary samples)...")

        calibration_mags = []
        attempts = 0
        max_attempts = 300  # 30 seconds at ~10 Hz

        while len(calibration_mags) < 20 and attempts < max_attempts:
            test_data = self.accel_daemon.get_data(timeout=0.2)
            if test_data:
                x = float(test_data.get('x', 0))
                y = float(test_data.get('y', 0))
                z = float(test_data.get('z', 0))
                mag = (x**2 + y**2 + z**2) ** 0.5
                calibration_mags.append(mag)
            attempts += 1

        if calibration_mags:
            # Use median to filter outliers
            self.gravity = sorted(calibration_mags)[len(calibration_mags) // 2]
            print(f"  ✓ Gravity calibrated: {self.gravity:.2f} m/s²")
            print(f"    (collected {len(calibration_mags)} samples, range: {min(calibration_mags):.2f}-{max(calibration_mags):.2f})")
            self.calibration_complete = True
            return True
        else:
            print(f"  ⚠ Calibration failed, using default 9.81 m/s²")
            return False

    def start(self):
        print("\n" + "="*100)
        print("REAL-TIME FILTER COMPARISON: EKF vs Complementary")
        print("="*100)

        # CLEANUP: Give system time to release sensor resources from previous runs
        print("\n✓ Initializing sensor (brief pause for cleanup)...")
        time.sleep(0.5)

        if not self.accel_daemon.start():
            print("ERROR: Failed to start sensor daemon")
            return False

        print(f"\n✓ Accelerometer daemon started")

        # STARTUP VALIDATION - MANDATORY accelerometer data required
        print(f"\n✓ Validating sensor startup (waiting up to 10 seconds for accelerometer data)...")
        print(f"  [REQUIRED] Waiting for accelerometer samples...")

        accel_data_received = False
        for attempt in range(10):  # 10 attempts × 1 second = 10 second timeout
            test_data = self.accel_daemon.get_data(timeout=1.0)
            if test_data:
                print(f"  ✓ Accelerometer responding with data on attempt {attempt + 1}")
                accel_data_received = True
                break
            elif attempt < 9:
                print(f"  Waiting... (attempt {attempt + 1}/10)")

        if not accel_data_received:
            print(f"\n✗ FATAL ERROR: No accelerometer data received after 10 seconds")
            print(f"  Test cannot proceed without accelerometer input")
            print(f"  Check: termux-sensor -s ACCELEROMETER works manually")
            self.accel_daemon.stop()
            return False

        # CRITICAL: Calibrate gravity magnitude before starting filters
        if not self._calibrate_gravity():
            print(f"  ⚠ WARNING: Gravity calibration failed, using default value")
            print(f"  ⚠ Complementary filter may show velocity drift if device is not level")

        print(f"\n✓ EKF filter initialized")
        print(f"✓ Complementary filter initialized")

        # Start GPS daemon (continuous polling in background)
        print(f"\n✓ Starting GPS daemon (continuous polling)...")
        if not self.gps_daemon.start():
            print(f"  ⚠ WARNING: GPS daemon failed to start")
            print(f"  ⚠ Continuing test WITHOUT GPS (EKF will use Accel only)")
            self.gps_daemon = None  # Mark as unavailable for graceful degradation
        else:
            print(f"  ✓ GPS daemon started (polling termux-location continuously)")
            print(f"  ⏱ Allowing GPS backend 3 seconds to stabilize...")
            time.sleep(3)  # CRITICAL FIX: Allow API service to recover from cleanup

        # OPTIONAL: Initialize gyroscope if requested (uses shared IMU stream from accel_daemon)
        if self.enable_gyro:
            print(f"\n✓ Initializing gyroscope (optional, will fallback if unavailable)...")
            self.gyro_daemon = PersistentGyroDaemon(accel_daemon=self.accel_daemon, delay_ms=50)

            if not self.gyro_daemon.start():
                print(f"  ⚠ WARNING: Gyroscope daemon failed to start")
                print(f"  ⚠ Continuing test WITHOUT gyroscope (EKF will use GPS+Accel only)")
                self.gyro_daemon = None
                self.enable_gyro = False
            else:
                print(f"  ✓ Gyroscope daemon started (using shared IMU stream)")
                print(f"  Note: Gyroscope data will be collected during test run")

        if self.duration_minutes is None:
            print(f"\n✓ Running continuously (press Ctrl+C to stop)...")
        else:
            print(f"\n✓ Running for {self.duration_minutes} minutes...")

        # Start GPS thread
        gps_thread = threading.Thread(target=self._gps_loop, daemon=True)
        gps_thread.start()

        # Start accel thread
        accel_thread = threading.Thread(target=self._accel_loop, daemon=True)
        accel_thread.start()

        # Start HEALTH MONITOR thread (detects sensor silence and triggers restarts)
        health_thread = threading.Thread(target=self._health_monitor_loop, daemon=True)
        health_thread.start()

        # Start gyro thread (if enabled)
        if self.gyro_daemon:
            gyro_thread = threading.Thread(target=self._gyro_loop, daemon=True)
            gyro_thread.start()

        # Display thread
        display_thread = threading.Thread(target=self._display_loop, daemon=True)
        display_thread.start()

        # Wait for duration with periodic auto-save
        try:
            end_time = time.time() + (self.duration_minutes * 60 if self.duration_minutes else float('inf'))

            # Run with periodic auto-save for both timed and continuous modes
            while not self.stop_event.is_set() and time.time() < end_time:
                time.sleep(1)
                # Check if time to auto-save
                if time.time() - self.last_auto_save_time > self.auto_save_interval:
                    print(f"\n✓ Auto-saving data ({len(self.gps_samples)} GPS, {len(self.accel_samples)} accel samples)...")
                    self._save_results(auto_save=True, clear_after_save=True)
                    self.last_auto_save_time = time.time()
        except KeyboardInterrupt:
            print("\n\nInterrupted by user")
        finally:
            self.stop()

        return True

    def _gps_loop(self):
        """Read GPS data from daemon queue continuously (no blocking)"""
        while not self.stop_event.is_set():
            # Non-blocking read from GPS daemon queue (skip if daemon failed to start)
            if not self.gps_daemon:
                time.sleep(0.5)
                continue
            gps = self.gps_daemon.get_data(timeout=0.1)

            if gps:
                self.last_gps_sample_time = time.time()  # UPDATE HEALTH MONITOR
                try:
                    now = time.time()
                    # Update both filters with new GPS fix
                    v1, d1 = self.ekf.update_gps(gps['latitude'], gps['longitude'],
                                                  gps['speed'], gps['accuracy'])
                    v2, d2 = self.complementary.update_gps(gps['latitude'], gps['longitude'],
                                                            gps['speed'], gps['accuracy'])

                    # FIX 6: Increment cumulative GPS counter
                    self.total_gps_fixes += 1

                    self.gps_samples.append({
                        'timestamp': now - self.start_time,
                        'latitude': gps['latitude'],
                        'longitude': gps['longitude'],
                        'accuracy': gps['accuracy'],
                        'speed': gps['speed'],
                        'ekf_velocity': v1,
                        'ekf_distance': d1,
                        'comp_velocity': v2,
                        'comp_distance': d2
                    })
                except Exception as e:
                    print(f"ERROR in GPS loop at {time.time() - self.start_time:.2f}s: {e}", file=sys.stderr)
                    import traceback
                    traceback.print_exc()

            time.sleep(0.01)  # Brief sleep to avoid CPU spinning

    def _accel_loop(self):
        """Process accelerometer samples"""
        while not self.stop_event.is_set():
            accel_data = self.accel_daemon.get_data(timeout=0.1)

            if accel_data:
                self.last_accel_sample_time = time.time()  # UPDATE HEALTH MONITOR

                try:
                    # Data now comes pre-extracted as {'x': ..., 'y': ..., 'z': ...}
                    x = float(accel_data.get('x', 0))
                    y = float(accel_data.get('y', 0))
                    z = float(accel_data.get('z', 0))

                    raw_magnitude = (x**2 + y**2 + z**2) ** 0.5

                    # CRITICAL FIX: Subtract gravity magnitude to get true motion magnitude
                    # This prevents infinite velocity accumulation during stationary periods
                    # Raw magnitude is always ~9.81 when device is level (gravity)
                    motion_magnitude = max(0, raw_magnitude - self.gravity)

                    # Update both filters with gravity-corrected magnitude
                    v1, d1 = self.ekf.update_accelerometer(motion_magnitude)
                    v2, d2 = self.complementary.update_accelerometer(motion_magnitude)

                    self.accel_samples.append({
                        'timestamp': time.time() - self.start_time,
                        'magnitude': motion_magnitude,
                        'ekf_velocity': v1,
                        'ekf_distance': d1,
                        'comp_velocity': v2,
                        'comp_distance': d2
                    })
                except Exception as e:
                    print(f"ERROR in accel loop at {time.time() - self.start_time:.2f}s: {e}", file=sys.stderr)
                    import traceback
                    traceback.print_exc()

    def _health_monitor_loop(self):
        """Monitor sensor health and auto-restart if sensors go silent or die"""
        while not self.stop_event.is_set():
            time.sleep(self.health_check_interval)
            now = time.time()

            # CHECK ACCELEROMETER HEALTH
            if self.accel_daemon:
                # First check: Has the subprocess died? (not just silent data)
                # This catches clean exits (exit_code=0) that data silence might miss
                if not self.accel_daemon.is_alive():
                    exit_code = self.accel_daemon.sensor_process.poll() if self.accel_daemon.sensor_process else None
                    print(f"\n⚠️ ACCEL DAEMON DIED (exit_code={exit_code}) - triggering immediate restart", file=sys.stderr)
                    if self.restart_counts['accel'] < self.max_restart_attempts:
                        if self._restart_accel_daemon():
                            self.last_accel_sample_time = now
                            print(f"  ✓ Accel restarted after daemon death", file=sys.stderr)
                        else:
                            print(f"  ✗ Accel restart failed after daemon death", file=sys.stderr)
                else:
                    # Second check: Is there data silence? (process alive but no data)
                    silence_duration = now - self.last_accel_sample_time
                    if silence_duration > self.accel_silence_threshold:
                        if self.restart_counts['accel'] < self.max_restart_attempts:
                            print(f"\n⚠️ ACCEL SILENT for {silence_duration:.1f}s - triggering auto-restart", file=sys.stderr)
                            if self._restart_accel_daemon():
                                self.last_accel_sample_time = now
                                print(f"  ✓ Accel restarted, resuming data collection", file=sys.stderr)
                            else:
                                print(f"  ✗ Accel restart failed", file=sys.stderr)

            # CHECK GPS HEALTH
            if self.gps_daemon:
                # First check: Has the subprocess died?
                if not self.gps_daemon.is_alive():
                    exit_code = self.gps_daemon.gps_process.poll() if self.gps_daemon.gps_process else None
                    print(f"\n⚠️ GPS DAEMON DIED (exit_code={exit_code}) - triggering immediate restart", file=sys.stderr)
                    if self.restart_counts['gps'] < self.max_restart_attempts:
                        if self._restart_gps_daemon():
                            self.last_gps_sample_time = now
                            print(f"  ✓ GPS restarted after daemon death", file=sys.stderr)
                        else:
                            print(f"  ✗ GPS restart failed after daemon death", file=sys.stderr)
                else:
                    # Second check: Is there data silence?
                    silence_duration = now - self.last_gps_sample_time
                    if silence_duration > self.gps_silence_threshold:
                        if self.restart_counts['gps'] < self.max_restart_attempts:
                            print(f"\n⚠️ GPS SILENT for {silence_duration:.1f}s - triggering auto-restart", file=sys.stderr)
                            if self._restart_gps_daemon():
                                self.last_gps_sample_time = now
                                print(f"  ✓ GPS restarted, resuming data collection", file=sys.stderr)
                            else:
                                print(f"  ✗ GPS restart failed (continuing without GPS)", file=sys.stderr)

    def _gyro_loop(self):
        """Process gyroscope samples and feed to EKF filter (if enabled)"""
        import sys
        samples_collected = 0
        while not self.stop_event.is_set():
            # Skip if gyro not available
            if not self.gyro_daemon or not self.enable_gyro:
                time.sleep(0.5)
                continue

            gyro_data = self.gyro_daemon.get_data(timeout=0.1)

            if gyro_data:
                samples_collected += 1
                if samples_collected <= 1:
                    print(f"[GYRO] First sample received: {list(gyro_data.keys())}", file=sys.stderr)
                try:
                    # Extract gyroscope angular velocities (rad/s)
                    # Data now comes pre-extracted as {'x': ..., 'y': ..., 'z': ...}
                    gyro_x = float(gyro_data.get('x', 0))  # rad/s
                    gyro_y = float(gyro_data.get('y', 0))  # rad/s
                    gyro_z = float(gyro_data.get('z', 0))  # rad/s

                    magnitude = (gyro_x**2 + gyro_y**2 + gyro_z**2) ** 0.5

                    # Update EKF filter with gyroscope data
                    # (Complementary filter does NOT support gyroscope)
                    v1, d1 = self.ekf.update_gyroscope(gyro_x, gyro_y, gyro_z)

                    # Collect validation metrics (gyro bias convergence, quaternion health, etc.)
                    if self.metrics:
                        ekf_state = self.ekf.get_state()
                        # Get latest GPS heading if available
                        gps_heading = None
                        if self.gps_samples:
                            latest_gps = self.gps_samples[-1]
                            if 'bearing' in latest_gps or 'heading' in latest_gps:
                                gps_heading = latest_gps.get('bearing', latest_gps.get('heading'))

                        # Get latest accelerometer magnitude for incident detection
                        accel_magnitude = 0
                        if self.accel_samples:
                            accel_magnitude = self.accel_samples[-1]['magnitude']

                        self.metrics.update(
                            ekf_state=ekf_state,
                            gyro_measurement=[gyro_x, gyro_y, gyro_z],
                            gps_heading=gps_heading,
                            accel_magnitude=accel_magnitude
                        )

                    # Store gyroscope sample for analysis
                    self.gyro_samples.append({
                        'timestamp': time.time() - self.start_time,
                        'gyro_x': gyro_x,
                        'gyro_y': gyro_y,
                        'gyro_z': gyro_z,
                        'magnitude': magnitude,
                        'ekf_velocity': v1,
                        'ekf_distance': d1
                    })
                except Exception as e:
                    print(f"ERROR in gyro loop at {time.time() - self.start_time:.2f}s: {e}", file=sys.stderr)
                    import traceback
                    traceback.print_exc()

            # Brief sleep to avoid CPU spinning
            time.sleep(0.01)

    def _display_loop(self):
        """Display metrics every second, log status every 30 seconds"""
        last_display = 0
        last_status_log = 0

        while not self.stop_event.is_set():
            now = time.time()

            # Log status every 30 seconds (to stderr)
            if now - last_status_log > 30.0:
                last_status_log = now
                self._log_status()

            # Display metrics every second
            if now - last_display > 1.0:
                last_display = now
                self._display_metrics()

            time.sleep(0.1)

    def _restart_accel_daemon(self):
        """Attempt to restart the accelerometer daemon"""
        print(f"\n🔄 Attempting to restart accelerometer daemon (attempt {self.restart_counts['accel'] + 1}/{self.max_restart_attempts})...", file=sys.stderr)

        # AGGRESSIVE STOP: Kill old daemon processes completely
        try:
            self.accel_daemon.stop()
            # Force kill termux-sensor and termux-api to fully clean up
            os.system("pkill -9 termux-sensor 2>/dev/null")
            os.system("pkill -9 termux-api 2>/dev/null")
            time.sleep(3)  # EXTENDED pause for kernel cleanup
        except Exception as e:
            print(f"  Warning during accel daemon stop: {e}", file=sys.stderr)

        # Create new daemon instance
        self.accel_daemon = PersistentAccelDaemon(delay_ms=50)

        # EXTENDED cooldown for full resource release
        time.sleep(self.restart_cooldown + 2)

        # Start new daemon
        if self.accel_daemon.start():
            # Validate it's actually working (EXTENDED timeout: termux-sensor needs full init on restart)
            test_data = self.accel_daemon.get_data(timeout=15.0)  # INCREASED from 10 to 15 seconds
            if test_data:
                print(f"  ✓ Accelerometer daemon restarted successfully", file=sys.stderr)
                self.restart_counts['accel'] += 1
                return True
            else:
                print(f"  ✗ Accelerometer daemon started but not receiving data after 15s (termux-sensor may be unresponsive)", file=sys.stderr)
                return False
        else:
            print(f"  ✗ Failed to start accelerometer daemon process", file=sys.stderr)
            return False

    def _restart_gps_daemon(self):
        """Attempt to restart the GPS daemon"""
        print(f"\n🔄 Attempting to restart GPS daemon (attempt {self.restart_counts['gps'] + 1}/{self.max_restart_attempts})...", file=sys.stderr)

        # Stop old daemon
        try:
            self.gps_daemon.stop()
            time.sleep(1)  # Brief pause for cleanup
        except Exception as e:
            print(f"  Warning during GPS daemon stop: {e}", file=sys.stderr)

        # Create new daemon instance
        self.gps_daemon = PersistentGPSDaemon()

        # Wait for cooldown
        time.sleep(self.restart_cooldown)

        # Start new daemon
        if self.gps_daemon.start():
            print(f"  ✓ GPS daemon restarted successfully", file=sys.stderr)
            self.restart_counts['gps'] += 1
            return True
        else:
            print(f"  ✗ Failed to restart GPS daemon", file=sys.stderr)
            return False

    def _log_status(self):
        """Log status update to stderr (won't clutter display)"""
        elapsed = time.time() - self.start_time
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)

        # Memory
        mem_info = self.process.memory_info()
        mem_mb = mem_info.rss / 1024 / 1024
        self.peak_memory = max(self.peak_memory, mem_mb)

        # Sample counts
        gps_count = len(self.gps_samples)
        accel_count = len(self.accel_samples)
        gyro_count = len(self.gyro_samples)

        # ⚠️ CRITICAL: Check daemon health every 30 seconds
        accel_status = self.accel_daemon.get_status()
        gps_status = self.gps_daemon.get_status() if self.gps_daemon else "DISABLED"

        status_msg = (
            f"[{mins:02d}:{secs:02d}] STATUS: Memory={mem_mb:.1f}MB (peak={self.peak_memory:.1f}MB) | "
            f"GPS={gps_count:4d} ({gps_status}) | Accel={accel_count:5d} ({accel_status})"
        )

        if self.enable_gyro:
            status_msg += f" | Gyro={gyro_count:5d}"

        # Add restart counts if any restarts occurred
        if self.restart_counts['accel'] > 0 or self.restart_counts['gps'] > 0:
            status_msg += f" | Restarts: Accel={self.restart_counts['accel']}, GPS={self.restart_counts['gps']}"

        sys.stderr.write(status_msg + "\n")
        sys.stderr.flush()

        # 🔄 AUTO-RESTART: If accelerometer daemon dies, attempt restart
        if accel_status.startswith("DEAD"):
            if self.restart_counts['accel'] < self.max_restart_attempts:
                warning_msg = (
                    f"\n⚠️  WARNING: Accelerometer daemon died at {mins:02d}:{secs:02d}\n"
                    f"   Status: {accel_status}\n"
                    f"   Samples collected: {accel_count}\n"
                    f"   Attempting automatic restart..."
                )
                print(warning_msg, file=sys.stderr)

                if self._restart_accel_daemon():
                    print(f"   ✓ Accelerometer daemon recovered, test continues\n", file=sys.stderr)
                else:
                    self.restart_counts['accel'] += 1  # Count failed attempt
                    print(f"   ✗ Restart attempt {self.restart_counts['accel']}/{self.max_restart_attempts} failed\n", file=sys.stderr)

                    # If we've hit max retries, fail the test
                    if self.restart_counts['accel'] >= self.max_restart_attempts:
                        error_msg = (
                            f"\n🚨 FATAL ERROR: Accelerometer daemon failed after {self.max_restart_attempts} restart attempts\n"
                            f"   This indicates a persistent sensor hardware issue or Termux:API failure.\n"
                            f"   Test cannot continue without accelerometer data."
                        )
                        print(error_msg, file=sys.stderr)
                        self.stop_event.set()  # Signal main loop to exit
                        return
            else:
                # Already hit max retries
                error_msg = (
                    f"\n🚨 FATAL ERROR: Accelerometer daemon still dead (max retries exceeded)\n"
                    f"   Test cannot continue without accelerometer data."
                )
                print(error_msg, file=sys.stderr)
                self.stop_event.set()  # Signal main loop to exit
                return

        # 🔄 AUTO-RESTART: GPS daemon died (test can continue with accel only, but try to recover)
        if gps_status.startswith("DEAD") and self.gps_daemon:
            if self.restart_counts['gps'] < self.max_restart_attempts:
                warning_msg = (
                    f"\n⚠️  WARNING: GPS daemon died at {mins:02d}:{secs:02d}\n"
                    f"   Status: {gps_status}\n"
                    f"   Samples collected: {gps_count}\n"
                    f"   Attempting automatic restart..."
                )
                print(warning_msg, file=sys.stderr)

                if self._restart_gps_daemon():
                    print(f"   ✓ GPS daemon recovered, test continues\n", file=sys.stderr)
                else:
                    self.restart_counts['gps'] += 1  # Count failed attempt
                    print(f"   ✗ Restart attempt {self.restart_counts['gps']}/{self.max_restart_attempts} failed\n", file=sys.stderr)

                    # If we've hit max retries, disable GPS but continue test
                    if self.restart_counts['gps'] >= self.max_restart_attempts:
                        warning_msg = (
                            f"\n⚠️  GPS daemon failed after {self.max_restart_attempts} restart attempts\n"
                            f"   Disabling GPS, continuing with accelerometer-only fusion."
                        )
                        print(warning_msg, file=sys.stderr)
                        self.gps_daemon = None  # Mark as unavailable
            else:
                # Already hit max retries, disable if not already done
                if self.gps_daemon:
                    print(f"\n⚠️  GPS daemon still dead (max retries exceeded), disabling GPS\n", file=sys.stderr)
                    self.gps_daemon = None  # Mark as unavailable

        # Print gyro-EKF validation metrics every 30 seconds (if enabled)
        if self.enable_gyro and self.metrics:
            self.metrics.print_dashboard(interval=30)

    def _display_metrics(self):
        """Show side-by-side comparison"""
        if not self.gps_samples and not self.accel_samples:
            return

        # Get latest state
        ekf_state = self.ekf.get_state()
        comp_state = self.complementary.get_state()

        elapsed = time.time() - self.start_time
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)

        # Get latest sensor data
        latest_accel = self.accel_samples[-1] if self.accel_samples else None
        latest_gps = self.gps_samples[-1] if self.gps_samples else None

        print(f"\n[{mins:02d}:{secs:02d}] FILTER COMPARISON")
        print("-" * 100)

        # Header
        print(f"{'METRIC':<25} | {'EKF':^20} | {'COMPLEMENTARY':^20} | {'DIFF':^15}")
        print("-" * 100)

        # Velocity
        ekf_vel = ekf_state['velocity']
        comp_vel = comp_state['velocity']
        vel_diff = abs(ekf_vel - comp_vel)
        print(f"{'Velocity (m/s)':<25} | {ekf_vel:>8.3f} m/s         | {comp_vel:>8.3f} m/s         | {vel_diff:>8.3f} m/s  ")

        # Distance
        ekf_dist = ekf_state['distance']
        comp_dist = comp_state['distance']
        dist_diff_pct = abs(ekf_dist - comp_dist) / max(ekf_dist, comp_dist, 0.001) * 100 if max(ekf_dist, comp_dist) > 0 else 0
        print(f"{'Distance (m)':<25} | {ekf_dist:>8.2f} m           | {comp_dist:>8.2f} m           | {dist_diff_pct:>6.2f}%      ")

        # Acceleration magnitude
        ekf_accel = ekf_state['accel_magnitude']
        comp_accel = comp_state['accel_magnitude']
        print(f"{'Accel Magnitude (m/s²)':<25} | {ekf_accel:>8.3f} m/s²        | {comp_accel:>8.3f} m/s²        | {abs(ekf_accel - comp_accel):>8.3f} m/s² ")

        # Status
        ekf_status = "MOVING" if not ekf_state['is_stationary'] else "STATIONARY"
        comp_status = "MOVING" if not comp_state['is_stationary'] else "STATIONARY"
        print(f"{'Status':<25} | {ekf_status:^20} | {comp_status:^20} | {'':^15}")

        # Sensor info
        print("-" * 100)
        # FIX 6: Show total GPS fixes (cumulative), not just recent window
        sensor_info = f"GPS fixes: {self.total_gps_fixes} (recent: {len(self.gps_samples)}) | Accel samples: {len(self.accel_samples)}"
        if self.enable_gyro:
            sensor_info += f" | Gyro samples: {len(self.gyro_samples)}"
        print(sensor_info)

    def stop(self):
        self.stop_event.set()
        self.accel_daemon.stop()
        self.gps_daemon.stop()

        # CRITICAL: Verify accelerometer data was collected
        # FIX 1: Check both accumulated_data and current deques
        total_accel_samples = 0
        if hasattr(self, '_accumulated_data'):
            total_accel_samples = len(self._accumulated_data['accel_samples'])
        total_accel_samples += len(self.accel_samples)

        if total_accel_samples == 0:
            print(f"\n✗ FATAL ERROR: Test completed but NO accelerometer samples were collected")
            print(f"  This indicates a sensor hardware or configuration problem")
            print(f"  Verify: termux-sensor -s ACCELEROMETER produces output")
            print(f"  Results will be saved but test is INVALID")
            print()

        self._save_results()

    def _save_results(self, auto_save=False, clear_after_save=False):
        """Save results to JSON file (with auto-save and clear support)"""
        if hasattr(self, 'start_time') and isinstance(self.start_time, float):
            timestamp = datetime.fromtimestamp(self.start_time).strftime("%Y%m%d_%H%M%S")
        elif hasattr(self, 'start_time'):
            timestamp = self.start_time.strftime("%Y%m%d_%H%M%S")
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_filename = os.path.join(SESSIONS_DIR, f"comparison_{timestamp}")

        results = {
            'test_duration': self.duration_minutes,
            'actual_duration': time.time() - self.start_time,
            'peak_memory_mb': self.peak_memory,
            'auto_save': auto_save,
            'gps_samples': list(self.gps_samples),  # Convert deque to list
            'accel_samples': list(self.accel_samples),  # Convert deque to list
            'gyro_samples': list(self.gyro_samples) if self.enable_gyro else [],  # Convert deque to list
            'final_metrics': {
                'ekf': self.ekf.get_state(),
                'complementary': self.complementary.get_state()
            }
        }

        if auto_save:
            # FIX 2: Acquire lock before accessing shared data structures
            with self._save_lock:
                # Auto-save appends to file to preserve all historical data
                # Initialize accumulated data on first auto-save
                if not hasattr(self, '_accumulated_data'):
                    self._accumulated_data = {
                        'gps_samples': [],
                        'accel_samples': [],
                        'gyro_samples': [],
                        'autosave_count': 0
                    }

                # Append current samples to accumulated history (don't overwrite)
                self._accumulated_data['gps_samples'].extend(list(self.gps_samples))
                self._accumulated_data['accel_samples'].extend(list(self.accel_samples))
                if self.enable_gyro:
                    self._accumulated_data['gyro_samples'].extend(list(self.gyro_samples))
                self._accumulated_data['autosave_count'] += 1

                # Write accumulated data with current metrics
                accumulated_results = {
                    'test_duration': self.duration_minutes,
                    'actual_duration': time.time() - self.start_time,
                    'peak_memory_mb': self.peak_memory,
                    'auto_save': auto_save,
                    'autosave_number': self._accumulated_data['autosave_count'],
                    'gps_samples': self._accumulated_data['gps_samples'],
                    'accel_samples': self._accumulated_data['accel_samples'],
                    'gyro_samples': self._accumulated_data['gyro_samples'],
                    'final_metrics': {
                        'ekf': self.ekf.get_state(),
                        'complementary': self.complementary.get_state()
                    }
                }

                filename = f"{base_filename}.json.gz"
                temp_filename = f"{filename}.tmp"

                with gzip.open(temp_filename, 'wt', encoding='utf-8') as f:
                    json.dump(accumulated_results, f, separators=(',', ':'))

                # Atomic rename
                os.rename(temp_filename, filename)

                # Clear samples after saving to free memory (deques stay bounded)
                if clear_after_save:
                    self.gps_samples.clear()
                    self.accel_samples.clear()
                    self.gyro_samples.clear()

                    # FIX 4: REMOVED filter reset - filters should maintain state across auto-saves
                    # Resetting velocity to 0 mid-test creates fake physics

                    gps_count = len(self._accumulated_data['gps_samples'])
                    accel_count = len(self._accumulated_data['accel_samples'])
                    print(f"✓ Auto-saved (autosave #{self._accumulated_data['autosave_count']}): {filename} | Total: {gps_count} GPS + {accel_count} accel | Deques cleared")
                else:
                    gps_count = len(self._accumulated_data['gps_samples'])
                    accel_count = len(self._accumulated_data['accel_samples'])
                    print(f"✓ Auto-saved (autosave #{self._accumulated_data['autosave_count']}): {filename} | Total: {gps_count} GPS + {accel_count} accel")
        else:
            # FIX 1: Final save should use accumulated_data if it exists
            # (deques may be empty after auto-saves)
            with self._save_lock:
                if hasattr(self, '_accumulated_data') and self._accumulated_data['autosave_count'] > 0:
                    # Use accumulated data from all auto-saves + current deque contents
                    final_gps = self._accumulated_data['gps_samples'] + list(self.gps_samples)
                    final_accel = self._accumulated_data['accel_samples'] + list(self.accel_samples)
                    final_gyro = self._accumulated_data['gyro_samples'] + list(self.gyro_samples)

                    results['gps_samples'] = final_gps
                    results['accel_samples'] = final_accel
                    results['gyro_samples'] = final_gyro
                    results['total_autosaves'] = self._accumulated_data['autosave_count']
                else:
                    # No auto-saves occurred, use current deques (already set in results dict above)
                    pass

            # Final save - both compressed and uncompressed
            # Uncompressed JSON for easy inspection
            filename_json = f"{base_filename}.json"
            temp_filename = f"{filename_json}.tmp"

            with open(temp_filename, 'w') as f:
                json.dump(results, f, indent=2)

            os.rename(temp_filename, filename_json)

            # Compressed for storage efficiency
            filename_gz = f"{base_filename}.json.gz"
            with gzip.open(filename_gz, 'wt', encoding='utf-8') as f:
                json.dump(results, f, separators=(',', ':'))

            # Export gyro-EKF validation metrics (if enabled)
            if self.enable_gyro and self.metrics:
                metrics_filename = filename_json.replace('comparison_', 'metrics_')
                self.metrics.export_metrics(metrics_filename)
                print(f"✓ Validation metrics saved to: {metrics_filename}")

            print(f"\n✓ Final results saved:")
            print(f"  {filename_json}")
            print(f"  {filename_gz}")
            print(f"✓ Peak memory usage: {self.peak_memory:.1f} MB")
            # Print summary only on final save
            self._print_summary()

    def _calculate_gps_ground_truth(self):
        """Calculate actual GPS ground truth distance using haversine formula.

        This accumulates the haversine distance between consecutive GPS points
        to get the true distance traveled based on GPS coordinates alone.
        """
        import math

        if len(self.gps_samples) < 2:
            return 0.0

        total_distance = 0.0
        for i in range(1, len(self.gps_samples)):
            prev_gps = self.gps_samples[i-1]
            curr_gps = self.gps_samples[i]

            lat1 = prev_gps['latitude']
            lon1 = prev_gps['longitude']
            lat2 = curr_gps['latitude']
            lon2 = curr_gps['longitude']

            # Haversine formula
            R = 6371000  # Earth radius in meters
            phi1 = math.radians(lat1)
            phi2 = math.radians(lat2)
            delta_phi = math.radians(lat2 - lat1)
            delta_lambda = math.radians(lon2 - lon1)

            a = (math.sin(delta_phi/2) ** 2 +
                 math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda/2) ** 2)
            c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

            distance_increment = R * c
            total_distance += distance_increment

        return total_distance

    def _print_summary(self):
        """Print final comparison summary"""
        print("\n" + "="*100)
        print("FINAL COMPARISON SUMMARY")
        print("="*100)

        ekf_state = self.ekf.get_state()
        comp_state = self.complementary.get_state()

        if self.gps_samples:
            first_gps = self.gps_samples[0]
            last_gps = self.gps_samples[-1]

            # CRITICAL FIX: Calculate GPS ground truth from actual coordinates
            # NOT from EKF's estimate (that defeats the purpose of validation)
            gps_distance = self._calculate_gps_ground_truth()
            ekf_distance = ekf_state['distance']
            comp_distance = comp_state['distance']

            ekf_error_pct = abs(ekf_distance - gps_distance) / max(gps_distance, 0.001) * 100 if gps_distance > 0 else 0
            comp_error_pct = abs(comp_distance - gps_distance) / max(gps_distance, 0.001) * 100 if gps_distance > 0 else 0

            print(f"\nDistance Accuracy (vs GPS ground truth):")
            print(f"  GPS Distance (Haversine): {gps_distance:.2f} m")
            print(f"  EKF Distance:             {ekf_distance:.2f} m (Error: {ekf_error_pct:.2f}%)")
            print(f"  Complementary Distance:   {comp_distance:.2f} m (Error: {comp_error_pct:.2f}%)")

            if ekf_error_pct < comp_error_pct:
                if comp_error_pct > 0:
                    improvement = ((comp_error_pct - ekf_error_pct) / comp_error_pct) * 100
                    print(f"\n  ✓ EKF is {improvement:.1f}% more accurate than Complementary")
                else:
                    print(f"\n  ✓ Both filters have zero error (perfect accuracy)")
            else:
                if comp_error_pct > 0:
                    degradation = ((ekf_error_pct - comp_error_pct) / comp_error_pct) * 100
                    print(f"\n  ⚠ EKF is {degradation:.1f}% less accurate than Complementary")
                else:
                    print(f"\n  ⚠ Complementary has zero error; EKF has {ekf_error_pct:.2f}% error")

        print(f"\nFinal Velocities:")
        print(f"  EKF:           {ekf_state['velocity']:.3f} m/s")
        print(f"  Complementary: {comp_state['velocity']:.3f} m/s")

        # Gyro statistics (if enabled)
        if self.enable_gyro and self.gyro_samples:
            print(f"\nGyroscope Statistics:")
            print(f"  Total samples:  {len(self.gyro_samples)}")

            # Calculate rotation rate statistics
            gyro_x_vals = [s['gyro_x'] for s in self.gyro_samples]
            gyro_y_vals = [s['gyro_y'] for s in self.gyro_samples]
            gyro_z_vals = [s['gyro_z'] for s in self.gyro_samples]
            magnitude_vals = [s['magnitude'] for s in self.gyro_samples]

            import statistics
            print(f"  X-rotation (rad/s):  mean={statistics.mean(gyro_x_vals):.4f}, max={max(gyro_x_vals):.4f}")
            print(f"  Y-rotation (rad/s):  mean={statistics.mean(gyro_y_vals):.4f}, max={max(gyro_y_vals):.4f}")
            print(f"  Z-rotation (rad/s):  mean={statistics.mean(gyro_z_vals):.4f}, max={max(gyro_z_vals):.4f}")
            print(f"  Overall magnitude:   mean={statistics.mean(magnitude_vals):.4f}, max={max(magnitude_vals):.4f}")
        elif self.enable_gyro:
            print(f"\nGyroscope: Enabled but NO samples collected")

        print("\n" + "="*100)


def main():
    duration = None  # default: continuous (None means run until interrupted)
    enable_gyro = False

    for arg in sys.argv[1:]:
        if arg == '--gyro':
            enable_gyro = True
        elif arg.isdigit():
            duration = int(arg)

    print(f"\nConfiguration:")
    if duration is None:
        print(f"  Duration: Continuous (press Ctrl+C to stop)")
    else:
        print(f"  Duration: {duration} minutes")
    print(f"  Gyroscope: {'Enabled' if enable_gyro else 'Disabled'}")
    print(f"\nStarting in 2 seconds...")
    time.sleep(2)

    test = FilterComparison(duration_minutes=duration, enable_gyro=enable_gyro)

    # Set up signal handlers for graceful shutdown (SIGINT, SIGTERM)
    import signal
    def signal_handler(signum, frame):
        print(f"\n✓ Received signal {signum}, gracefully shutting down...")
        test.stop_event.set()

    signal.signal(signal.SIGINT, signal_handler)   # Ctrl+C
    signal.signal(signal.SIGTERM, signal_handler)  # Kill signal

    test.start()


if __name__ == '__main__':
    main()
