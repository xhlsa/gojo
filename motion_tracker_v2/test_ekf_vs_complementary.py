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
import tracemalloc
import json
import os
import gzip
import sqlite3
import psutil
import numpy as np
import statistics
from queue import Queue, Empty, Full
from datetime import datetime
from collections import deque

# Try orjson for speed
try:
    import orjson
    HAS_ORJSON = True
except ImportError:
    HAS_ORJSON = False
    import json

# MEMORY OPTIMIZATION: Numpy structured arrays (32x reduction vs dicts)
# GPS: 768 bytes/sample (dict) → 40 bytes/sample (numpy) = 19x reduction
# Accel: 416 bytes/sample (dict) → 12 bytes/sample (numpy) = 35x reduction
# Gyro: 416 bytes/sample (dict) → 12 bytes/sample (numpy) = 35x reduction

GPS_DTYPE = np.dtype([
    ('timestamp', 'f8'),    # 8 bytes (double precision for sub-second accuracy)
    ('latitude', 'f8'),     # 8 bytes (high precision for GPS coords)
    ('longitude', 'f8'),    # 8 bytes
    ('accuracy', 'f4'),     # 4 bytes (float32 sufficient for accuracy in meters)
    ('speed', 'f4'),        # 4 bytes (float32 sufficient for speed in m/s)
    ('provider', 'U8')      # 8 bytes (8-char unicode string, e.g., "gps", "network")
])  # Total: 40 bytes per GPS sample

ACCEL_DTYPE = np.dtype([
    ('timestamp', 'f8'),    # 8 bytes
    ('magnitude', 'f4')     # 4 bytes (float32 sufficient for acceleration in m/s²)
])  # Total: 12 bytes per accel sample

GYRO_DTYPE = np.dtype([
    ('timestamp', 'f8'),    # 8 bytes
    ('magnitude', 'f4')     # 4 bytes (float32 sufficient for rotation rate in rad/s)
])  # Total: 12 bytes per gyro sample

TRAJECTORY_DTYPE = np.dtype([
    ('timestamp', 'f8'),    # 8 bytes (relative timestamp)
    ('lat', 'f8'),          # 8 bytes latitude
    ('lon', 'f8'),          # 8 bytes longitude
    ('velocity', 'f4'),     # 4 bytes velocity magnitude
    ('uncertainty', 'f4')   # 4 bytes position uncertainty (meters)
])  # Total: 32 bytes per trajectory point

COVARIANCE_DTYPE = np.dtype([
    ('timestamp', 'f8'),  # Timestamp of snapshot
    ('trace', 'f8'),      # Covariance trace for quick sanity checks
    ('p00', 'f8'),        # First diagonal entry
    ('p11', 'f8'),
    ('p22', 'f8'),
    ('p33', 'f8'),
    ('p44', 'f8'),
    ('p55', 'f8')
])  # Stores leading diagonal entries for regression analysis

from filters import get_filter
from motion_tracker_v2 import PersistentAccelDaemon, PersistentGyroDaemon
from metrics_collector import MetricsCollector
from incident_detector import IncidentDetector

# Session directory for organized data storage (matches motion_tracker_v2.py pattern)
SESSIONS_DIR = os.path.join(os.path.dirname(__file__), "..", "motion_tracker_sessions")
if not os.path.exists(SESSIONS_DIR):
    os.makedirs(SESSIONS_DIR, exist_ok=True)


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
        self.last_fix_time = time.time()
        self._watchdog_thread = None
        self._watchdog_warning_logged = False

    def start(self):
        """Start GPS daemon that continuously polls termux-location"""
        try:
            # CRITICAL: Clear stop_event before starting (allows restart after stop())
            self.stop_event.clear()

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
import sys
import time

# GPS polling wrapper - poll every 5 seconds (matches Termux:API hardcoded minimum)
# Note: termux-location -r updates does NOT actually stream continuously on this device
# It outputs one fix then exits, so we use polling with -p gps instead

next_poll_time = time.time()
last_success_time = time.time()
warn_interval = 30.0
max_starvation = 120.0
warned = False
max_runtime = 2700    # 45 minutes max
request_count = 0
success_count = 0

sys.stderr.write("[GPS Wrapper] Starting (poll_interval=5s, max_starvation=300.0s)\\n")
sys.stderr.flush()

while time.time() - next_poll_time < max_runtime:
    current_time = time.time()

    # Poll every 5 seconds
    if current_time >= next_poll_time:
        try:
            request_count += 1
            result = subprocess.run(
                ['termux-location', '-p', 'gps'],
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode == 0 and result.stdout.strip():
                success_count += 1
                # CRITICAL: Output compact JSON on single line (termux-location may output pretty-printed JSON)
                # Parse and re-serialize as compact JSON to ensure _read_loop can parse line-by-line
                try:
                    import json
                    gps_json = json.loads(result.stdout)
                    compact_json = json.dumps(gps_json, separators=(',', ':'))
                    print(compact_json, flush=True)
                    sys.stderr.write(f"[GPS] ✓ Fix #{success_count} acquired\\n")
                    sys.stderr.flush()
                except json.JSONDecodeError as je:
                    # Log JSON parsing errors but output as-is
                    sys.stderr.write(f"[GPS] Warning: JSON parse error {je}, outputting raw\\n")
                    sys.stderr.flush()
                    print(result.stdout, flush=True)
                except Exception as e:
                    # Log any other errors during JSON processing
                    sys.stderr.write(f"[GPS] Warning: Error processing GPS output: {type(e).__name__}: {e}\\n")
                    sys.stderr.flush()
                    print(result.stdout, flush=True)
                last_success_time = time.time()
            else:
                sys.stderr.write(f"[GPS] No output (code {result.returncode})\\n")
                sys.stderr.flush()

        except subprocess.TimeoutExpired:
            sys.stderr.write("[GPS] ✗ Request timeout (30s)\\n")
            sys.stderr.flush()
        except Exception as e:
            sys.stderr.write(f"[GPS] Error: {e}\\n")
            sys.stderr.flush()

        next_poll_time = current_time + 5.0

    # Check for starvation
    starved_for = current_time - last_success_time
    if starved_for > max_starvation:
        sys.stderr.write(f"[GPS] ⚠️  STARVATION: No GPS data for {int(starved_for)}s (requests={request_count}, successes={success_count})\\n")
        sys.stderr.flush()
        last_success_time = current_time
        warned = True
    elif starved_for > warn_interval and not warned:
        sys.stderr.write(f"[GPS] Warning: No GPS fix for {int(starved_for)}s\\n")
        sys.stderr.flush()
        warned = True
    elif starved_for <= warn_interval:
        warned = False

    time.sleep(0.1)
'''

            self.gps_process = subprocess.Popen(
                ['python3', '-c', wrapper_script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                close_fds=True  # CRITICAL: Close inherited file descriptors to prevent leaks
            )

            # Start separate thread to capture stderr from GPS daemon (includes Termux API errors)
            stderr_reader = threading.Thread(target=self._capture_stderr, daemon=True)
            stderr_reader.start()

            reader = threading.Thread(target=self._read_loop, daemon=True)
            reader.start()

            if self._watchdog_thread is None or not self._watchdog_thread.is_alive():
                self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
                self._watchdog_thread.start()
            return True
        except Exception as e:
            print(f"Failed to start GPS daemon: {e}", file=sys.stderr)
            return False

    def _capture_stderr(self):
        """Capture stderr from GPS daemon to detect API/connection errors"""
        try:
            for line in self.gps_process.stderr:
                line = line.strip()
                if line:
                    # Log any stderr output - likely error messages or connection issues
                    print(f"[GPSDaemon] stderr: {line}", file=sys.stderr)
        except StopIteration:
            # Normal end of stream when process closes stderr
            pass
        except Exception as e:
            # Log unexpected errors in stderr reading (but don't crash)
            print(f"[GPSDaemon] Error reading stderr: {type(e).__name__}: {e}", file=sys.stderr)

    def _read_loop(self):
        """Read GPS JSON objects from continuous stream (line-by-line)"""
        import sys
        fix_count = 0

        print(f"[GPS _read_loop] Thread started", file=sys.stderr)
        sys.stderr.flush()

        try:
            # termux-location outputs compact JSON on single lines
            # Read line-by-line instead of complex brace counting
            for line in self.gps_process.stdout:
                if self.stop_event.is_set():
                    break

                line = line.strip()
                if not line:
                    continue  # Skip empty lines

                try:
                    # Parse JSON from complete line
                    data = json.loads(line)

                    # Extract GPS data
                    gps_data = {
                        'latitude': float(data.get('latitude')),
                        'longitude': float(data.get('longitude')),
                        'accuracy': float(data.get('accuracy', 5.0)),
                        'altitude': float(data.get('altitude', 0)),
                        'bearing': float(data.get('bearing', 0)),
                        'speed': float(data.get('speed', 0)),
                        'provider': 'gps'  # Track GPS provider (test uses -p gps only)
                    }

                    # Queue the fix
                    try:
                        self.data_queue.put_nowait(gps_data)
                        fix_count += 1
                        queue_size = self.data_queue.qsize()
                        self.last_fix_time = time.time()
                        self._watchdog_warning_logged = False
                        print(f"[GPS _read_loop] Fix #{fix_count} queued (lat={gps_data['latitude']:.4f}, queue_size={queue_size}, queue_id={id(self.data_queue)})", file=sys.stderr)
                        sys.stderr.flush()
                    except Exception as e:
                        print(f"[GPS _read_loop] ✗ Failed to queue fix: {e}", file=sys.stderr)
                        sys.stderr.flush()

                except json.JSONDecodeError as e:
                    # Log JSON parse errors (important for debugging)
                    print(f"[GPS _read_loop] ✗ JSON parse error: {e}, line='{line[:50]}'", file=sys.stderr)
                    sys.stderr.flush()
                except Exception as e:
                    # Log unexpected processing errors instead of silently dropping
                    print(f"[GPS _read_loop] ⚠️  Unexpected error processing GPS data: {type(e).__name__}: {e}", file=sys.stderr)
                    sys.stderr.flush()

        except Exception as e:
            # Log fatal errors only
            print(f"⚠️  [GPSDaemon] Reader thread error: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc(file=sys.stderr)

        print(f"[GPS _read_loop] Thread exiting (fixes_queued={fix_count})", file=sys.stderr)
        sys.stderr.flush()

    def _watchdog_loop(self):
        """Warn when GPS fixes stall (likely due to Termux backgrounding)."""
        while not self.stop_event.is_set():
            elapsed = time.time() - self.last_fix_time
            if elapsed > 30 and not self._watchdog_warning_logged:
                print(f"[GPSDaemon] Warning: No GPS fix for {int(elapsed)} seconds. Keep Termux foreground or rerun drive.sh to refresh the job.", file=sys.stderr)
                self._watchdog_warning_logged = True
            time.sleep(5)

    def get_data(self, timeout=0.1):
        """Non-blocking read from GPS queue"""
        try:
            result = self.data_queue.get(timeout=timeout)
            return result
        except Empty:
            # DEBUG: Log queue state when empty
            import sys
            queue_size = self.data_queue.qsize()
            if queue_size > 0:
                print(f"[GPS get_data] ⚠️  Queue has {queue_size} items but get() raised Empty! Queue ID: {id(self.data_queue)}", file=sys.stderr)
                sys.stderr.flush()
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
        """Stop GPS daemon (with explicit FD cleanup to prevent leaks)"""
        self.stop_event.set()
        if self.gps_process:
            try:
                self.gps_process.terminate()
                self.gps_process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                # Force kill if timeout
                self.gps_process.kill()
                self.gps_process.wait(timeout=1)
            except Exception:
                pass
            finally:
                # CRITICAL: Close file descriptors explicitly to prevent FD leak
                try:
                    if self.gps_process.stdout:
                        self.gps_process.stdout.close()
                    if self.gps_process.stderr:
                        self.gps_process.stderr.close()
                    if self.gps_process.stdin:
                        self.gps_process.stdin.close()
                except Exception:
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
        # Start memory profiling
        tracemalloc.start()

        self.start_time = time.time()  # Initialize early for session_timestamp
        self.duration_minutes = duration_minutes
        self.enable_gyro = enable_gyro
        self.stop_event = threading.Event()

        # Filters
        self.ekf = get_filter('ekf', enable_gyro=enable_gyro)
        self.complementary = get_filter('complementary')
        # Force Python backend so GPS velocity smoothing stays available even when Rust extension is installed
        self.es_ekf = get_filter('es_ekf', enable_gyro=enable_gyro, force_python=True)  # NEW: ES-EKF for trajectory mapping
        self.motion_profiles = {
            'vehicle': {
                'gps_noise': 8.0,
                'accel_noise': 0.5,
                'gps_velocity_noise': 1.5,
                'emit_interval': 1.0,
            },
            'pedestrian': {
                'gps_noise': 2.0,
                'accel_noise': 0.1,
                'gps_velocity_noise': 0.4,
                'emit_interval': 0.3,
            },
        }
        self.motion_profile = 'vehicle'
        self.pedestrian_speed_threshold = 2.0
        self._profile_speed_samples = deque(maxlen=30)
        self._apply_motion_profile(self.motion_profile)

        # Sensors (accelerometer and gyroscope are paired from same IMU hardware)
        # LSM6DSO hardware tested: 647 Hz @ 1ms (60% eff), 164 Hz @ 5ms (80% eff), 44 Hz @ 20ms (80% eff)
        # Using 20ms delay for 2.5x data rate vs old 50ms, still safe for memory (96-97 MB peak expected)
        self.accel_daemon = PersistentAccelDaemon(delay_ms=20)
        self.gps_daemon = PersistentGPSDaemon()  # Continuous GPS polling daemon
        self.gyro_daemon = None  # Will be initialized if enable_gyro=True

        # Data storage - NUMPY ARRAYS for 32x memory reduction
        # Pre-allocated structured arrays with index counters
        # GPS: 1000 fixes @ 0.2 Hz = ~83 minutes (40 bytes/sample = 40 KB vs 768 KB with dicts)
        # Accel: 150,000 samples @ 44 Hz actual = 57 minutes (12 bytes/sample = 1.8 MB vs 62 MB with dicts)
        # Gyro: 150,000 samples @ 44 Hz actual = 57 minutes (12 bytes/sample = 1.8 MB vs 62 MB with dicts)
        # Total: ~3.6 MB vs ~125 MB with dicts = 35x reduction

        self.max_gps_samples = 1000
        self.max_accel_samples = 150000
        self.max_gyro_samples = 150000

        self.gps_samples = np.zeros(self.max_gps_samples, dtype=GPS_DTYPE)
        self.accel_samples = np.zeros(self.max_accel_samples, dtype=ACCEL_DTYPE)
        self.gyro_samples = np.zeros(self.max_gyro_samples, dtype=GYRO_DTYPE)

        # Index counters to track current position in arrays
        self.gps_index = 0
        self.accel_index = 0
        self.gyro_index = 0

        # Trajectory buffers (ring buffers + chunk persistence for full-route exports)
        self.max_trajectory_points = 5000  # ~80 minutes @ 1 Hz before chunk flush
        self.max_covariance_snapshots = 2000

        self.trajectory_buffers = {
            'ekf': np.zeros(self.max_trajectory_points, dtype=TRAJECTORY_DTYPE),
            'es_ekf': np.zeros(self.max_trajectory_points, dtype=TRAJECTORY_DTYPE),
            'complementary': np.zeros(self.max_trajectory_points, dtype=TRAJECTORY_DTYPE),
            'es_ekf_dead_reckoning': np.zeros(self.max_trajectory_points, dtype=TRAJECTORY_DTYPE)
        }
        self.trajectory_indices = {key: 0 for key in self.trajectory_buffers}
        self.trajectory_total_counts = {key: 0 for key in self.trajectory_buffers}
        self.trajectory_chunk_paths = {key: [] for key in self.trajectory_buffers}

        self.covariance_buffer = np.zeros(self.max_covariance_snapshots, dtype=COVARIANCE_DTYPE)
        self.covariance_index = 0
        self.covariance_total_count = 0
        self.covariance_chunk_paths = []

        # Directory for persisted chunk files (per session)
        self.session_timestamp = datetime.fromtimestamp(self.start_time).strftime("%Y%m%d_%H%M%S")
        self.buffer_chunk_dir = os.path.join(SESSIONS_DIR, 'buffer_chunks', self.session_timestamp)
        os.makedirs(self.buffer_chunk_dir, exist_ok=True)
        self._sensor_db_conn = None
        self._sensor_db_path = os.path.join(self.buffer_chunk_dir, "sensor_cache.sqlite")
        self._init_sensor_cache()

        # FIX 2: Thread lock for accumulated_data and buffer operations
        self._save_lock = threading.RLock()
        self._last_traj_emit = {key: 0.0 for key in self.trajectory_buffers}
        # Emit synthetic ES-EKF points at profile-defined cadence
        self.dead_reckoning_emit_interval = self.motion_profiles[self.motion_profile]['emit_interval']
        self._last_es_ekf_gps_ts = None
        self._last_gps_fix_count = 0
        self._last_gps_fix_time = time.time()
        self._gps_cadence_warning_logged = False

        # Thread lock for GPS counter (thread-safe increment)
        self._gps_counter_lock = threading.Lock()

        # PHASE 1: Raw sensor data queues (producers: sensor daemons, consumers: collection loops)
        self.accel_raw_queue = Queue(maxsize=100)  # ~5s buffer @ 20Hz
        self.gps_raw_queue = Queue(maxsize=100)     # ~100s buffer @ 1Hz
        self.gyro_raw_queue = Queue(maxsize=100)   # ~5s buffer @ 20Hz

        # PHASE 1: Per-filter input queues (producers: collection loops, consumers: filter threads)
        # MEMORY OPTIMIZATION: Reduced from 500 to 100 (~5s buffer @ 20Hz instead of 25s)
        # Filters process in <100ms, so 5s buffer is plenty for temporary spikes
        self.ekf_accel_queue = Queue(maxsize=100)
        self.ekf_gps_queue = Queue(maxsize=50)
        self.ekf_gyro_queue = Queue(maxsize=100)

        self.comp_accel_queue = Queue(maxsize=100)
        self.comp_gps_queue = Queue(maxsize=50)

        self.es_ekf_accel_queue = Queue(maxsize=100)
        self.es_ekf_gps_queue = Queue(maxsize=50)
        self.es_ekf_gyro_queue = Queue(maxsize=100)

        # Thread lock for filter state (prevents _display_metrics from reading mid-update)
        self.state_lock = threading.Lock()

        # Metrics
        self.last_gps_time = None
        self.start_time = time.time()
        self.last_status_time = time.time()
        self.last_auto_save_time = time.time()

        # Memory monitoring
        self.process = psutil.Process()
        self.peak_memory = 0
        self.es_ekf_paused = False  # (legacy switch) no longer pauses ES-EKF

        # Auto-save configuration
        # MEMORY OPTIMIZATION: Reduced 15s → 5s to clear accumulated_data more frequently
        self.auto_save_interval = 5  # Save every 5 seconds (reduces peak memory by ~10-15 MB)

        # Metrics collector (for gyro-EKF validation)
        self.metrics = None
        if enable_gyro:
            self.metrics = MetricsCollector(max_history=600)

        # Incident detector (for swerving and hard braking detection)
        incident_dir = os.path.join(SESSIONS_DIR, 'incidents')
        os.makedirs(incident_dir, exist_ok=True)
        self.incident_detector = IncidentDetector(session_dir=incident_dir, sensor_sample_rate=20)

        # Daemon restart tracking
        self.restart_counts = {
            'accel': 0,
            'gps': 0,
            'gyro': 0
        }
        self.max_restart_attempts = 60  # Increased from 3: allows continuous GPS restart attempts during long tests
        # GPS validation will reject hung termux-location, but accel fallback works fine
        # Allows test to recover GPS when service becomes available again
        self.restart_cooldown = 10  # INCREASED from 5s → 10s (termux-sensor needs full resource release)

        # Thread locks for sensor restart (prevents concurrent restarts from health monitor + status logger)
        self._accel_restart_lock = threading.Lock()
        self._gps_restart_lock = threading.Lock()

        # HEALTH MONITORING: Detect sensor silence and auto-restart
        self.last_accel_sample_time = time.time()
        self.last_gps_sample_time = time.time()
        self.last_gyro_sample_time = time.time()
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

        # GPS health metrics
        self.gps_first_fix_latency = None  # Time from start to first GPS fix
        self.gps_first_fix_received = False  # Flag for first fix

        # Live status file for dashboard monitoring (file-based IPC)
        self.status_file = os.path.join(SESSIONS_DIR, 'live_status.json')
        self.last_status_update = time.time()

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
            print(f"  ⏱ Waiting for first GPS fix (up to 30 seconds)...")

            # PRE-TEST VALIDATION: Wait for actual first GPS fix before starting test
            # This ensures GPS is not just running, but actually collecting location data
            gps_lock_timeout = 30  # seconds
            start_wait = time.time()
            first_fix_received = False

            while time.time() - start_wait < gps_lock_timeout and not first_fix_received:
                gps_data = self.gps_daemon.get_data(timeout=1.0)
                if gps_data:
                    elapsed = time.time() - start_wait
                    print(f"  ✓ GPS lock acquired: {gps_data['latitude']:.4f}, {gps_data['longitude']:.4f} (latency: {elapsed:.1f}s)")
                    first_fix_received = True
                    # CRITICAL FIX: Process this first GPS fix immediately instead of discarding it
                    # This ensures we don't waste the first valid location data
                    try:
                        now = time.time()
                        # Update both filters with first GPS fix
                        v1, d1 = self.ekf.update_gps(gps_data['latitude'], gps_data['longitude'],
                                                      gps_data['speed'], gps_data['accuracy'])
                        v2, d2 = self.complementary.update_gps(gps_data['latitude'], gps_data['longitude'],
                                                                gps_data['speed'], gps_data['accuracy'])

                        # Add GPS sample for incident context
                        self.incident_detector.add_gps_sample(
                            gps_data['latitude'], gps_data['longitude'], gps_data['speed'], gps_data['accuracy'], timestamp=now
                        )

                        # Increment GPS counter (thread-safe)
                        with self._gps_counter_lock:
                            self.total_gps_fixes += 1

                        # Record first fix latency
                        self.gps_first_fix_latency = now - self.start_time
                        self.gps_first_fix_received = True

                        # Store GPS sample
                        if self.gps_index < self.max_gps_samples:
                            self.gps_samples[self.gps_index] = (
                                now - self.start_time,
                                gps_data['latitude'],
                                gps_data['longitude'],
                                gps_data['accuracy'],
                                gps_data['speed'],
                                gps_data.get('provider', 'gps')[:8]  # Truncate to 8 chars for U8 dtype
                            )
                            self.gps_index += 1
                        print(f"  ✓ First GPS fix processed and added to test data")
                    except Exception as e:
                        print(f"  ⚠ ERROR processing first GPS fix: {e}", file=sys.stderr)

                    break
                else:
                    elapsed = time.time() - start_wait
                    print(f"  Waiting for GPS... ({elapsed:.1f}s)")

            if not first_fix_received:
                print(f"  ⚠ WARNING: No GPS fix received after {gps_lock_timeout} seconds")
                print(f"  ⚠ GPS may be unavailable or location service not responding")
                print(f"  ⚠ Continuing test WITHOUT GPS (EKF will use Accel only)")

        # OPTIONAL: Initialize gyroscope if requested (shared IMU stream from accel daemon)
        if self.enable_gyro:
            print(f"\n✓ Initializing gyroscope (optional, will fallback if unavailable)...")
            # CRITICAL: Pass accel_daemon to share the same IMU hardware stream
            # Accelerometer and Gyroscope are paired sensors on LSM6DSO chip
            self.gyro_daemon = PersistentGyroDaemon(accel_daemon=self.accel_daemon, delay_ms=20)

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

        # PHASE 4: Start filter processing threads (INDEPENDENT - NEW)
        ekf_thread = threading.Thread(target=self._ekf_filter_thread, daemon=True, name="EKF_Filter")
        ekf_thread.start()

        comp_thread = threading.Thread(target=self._complementary_filter_thread, daemon=True, name="Comp_Filter")
        comp_thread.start()

        es_ekf_thread = threading.Thread(target=self._es_ekf_filter_thread, daemon=True, name="ES_EKF_Filter")
        es_ekf_thread.start()

        # Display thread
        display_thread = threading.Thread(target=self._display_loop, daemon=True)
        display_thread.start()

        # Wait for duration with periodic auto-save
        try:
            end_time = time.time() + (self.duration_minutes * 60 if self.duration_minutes else float('inf'))
            print(f"\n[DEBUG] Test start time: {self.start_time}, End time target: {end_time}, Duration: {self.duration_minutes} min ({self.duration_minutes * 60 if self.duration_minutes else 'inf'} sec)", file=sys.stderr)

            # Run with periodic auto-save for both timed and continuous modes
            while not self.stop_event.is_set() and time.time() < end_time:
                time.sleep(1)
                # Check if time to auto-save
                if time.time() - self.last_auto_save_time > self.auto_save_interval:
                    print(f"\n✓ Auto-saving data ({self.gps_index} GPS, {self.accel_index} accel samples)...")
                    self._save_results(auto_save=True, clear_after_save=True)
                    self.last_auto_save_time = time.time()

            # Log why the loop exited
            elapsed = time.time() - self.start_time
            print(f"\n[DEBUG] Loop exited after {elapsed:.1f}s. Stop event: {self.stop_event.is_set()}, Time check: {time.time() < end_time}", file=sys.stderr)

        except KeyboardInterrupt:
            print("\n\nInterrupted by user")
        finally:
            self.stop()

        return True

    def _gps_loop(self):
        """Read GPS data from daemon queue continuously (no blocking)"""
        import sys
        print(f"[GPS_LOOP] Started", file=sys.stderr)
        sys.stderr.flush()
        samples_processed = 0

        while not self.stop_event.is_set():
            # Non-blocking read from GPS daemon queue (skip if daemon failed to start)
            if not self.gps_daemon:
                if samples_processed == 0:  # Log once
                    print(f"[GPS_LOOP] ⚠️  gps_daemon is None, skipping queue reads", file=sys.stderr)
                    sys.stderr.flush()
                time.sleep(0.5)
                continue
            # Increased timeout to 1.0s to avoid missing fixes (GPS polls every 5s)
            gps = self.gps_daemon.get_data(timeout=1.0)

            # DEBUG: Log ALL get_data() attempts to diagnose consumption issue
            if gps is None and samples_processed < 20:  # Increased from 3 to 20 for more visibility
                queue_size = self.gps_daemon.data_queue.qsize()
                queue_id = id(self.gps_daemon.data_queue)
                print(f"[GPS_LOOP] get_data() returned None (check #{samples_processed+1}), queue_size={queue_size}, queue_id={queue_id}", file=sys.stderr)
                sys.stderr.flush()
            elif gps:
                print(f"[GPS_LOOP] ✓ get_data() SUCCESS (sample #{samples_processed+1}), lat={gps['latitude']:.4f}", file=sys.stderr)
                sys.stderr.flush()

            if gps:
                self.last_gps_sample_time = time.time()  # UPDATE HEALTH MONITOR
                try:
                    now = time.time()
                    timestamp_relative = now - self.start_time

                    # PHASE 2: Package GPS data with timestamp
                    gps_packet = {
                        'timestamp': timestamp_relative,
                        'latitude': gps['latitude'],
                        'longitude': gps['longitude'],
                        'speed': gps['speed'],
                        'accuracy': gps['accuracy'],
                        'provider': gps.get('provider', 'gps')
                    }

                    # PHASE 2: Distribute to ALL filter queues (non-blocking)
                    try:
                        self.ekf_gps_queue.put_nowait(gps_packet)
                    except Full:
                        # Queue full - drop this sample, filter thread will catch up on next data
                        pass
                    except Exception as e:
                        print(f"[GPS_LOOP] Error distributing to EKF GPS queue: {type(e).__name__}: {e}", file=sys.stderr)

                    try:
                        self.comp_gps_queue.put_nowait(gps_packet)
                    except Full:
                        pass
                    except Exception as e:
                        print(f"[GPS_LOOP] Error distributing to Complementary GPS queue: {type(e).__name__}: {e}", file=sys.stderr)

                    try:
                        self.es_ekf_gps_queue.put_nowait(gps_packet)
                    except Full:
                        pass
                    except Exception as e:
                        print(f"[GPS_LOOP] Error distributing to ES-EKF GPS queue: {type(e).__name__}: {e}", file=sys.stderr)

                    # Add GPS sample for incident context (30s before/after events)
                    self.incident_detector.add_gps_sample(
                        gps['latitude'], gps['longitude'], gps['speed'], gps['accuracy'], timestamp=now
                    )

                    # FIX 6: Increment cumulative GPS counter (thread-safe)
                    with self._gps_counter_lock:
                        self.total_gps_fixes += 1

                    samples_processed += 1
                    if samples_processed <= 5 or samples_processed % 5 == 0:
                        print(f"[GPS_LOOP] Queued sample #{samples_processed} (lat={gps['latitude']:.4f})", file=sys.stderr)
                        sys.stderr.flush()

                    # Create placeholder GPS sample (will be updated by filter threads)
                    with self._save_lock:
                        if self.gps_index < self.max_gps_samples:
                            self.gps_samples[self.gps_index] = (
                                timestamp_relative,
                                gps['latitude'],
                                gps['longitude'],
                                gps['accuracy'],
                                gps['speed'],
                                gps_packet['provider'][:8]  # Truncate to 8 chars max for U8 dtype
                            )
                            self.gps_index += 1
                        else:
                            print(f"⚠️ GPS buffer full ({self.max_gps_samples} samples), skipping", file=sys.stderr)
                except Exception as e:
                    print(f"ERROR in GPS loop at {time.time() - self.start_time:.2f}s: {e}", file=sys.stderr)
                    import traceback
                    traceback.print_exc()

            time.sleep(0.01)  # Brief sleep to avoid CPU spinning

    def _accel_loop(self):
        """Process accelerometer samples"""
        samples_processed = 0
        print("[ACCEL_LOOP] Started", file=sys.stderr)
        while not self.stop_event.is_set():
            accel_data = self.accel_daemon.get_data(timeout=0.1)

            if accel_data:
                self.last_accel_sample_time = time.time()  # UPDATE HEALTH MONITOR
                samples_processed += 1
                if samples_processed <= 5 or samples_processed % 100 == 0:
                    print(f"[ACCEL_LOOP] Processed sample #{samples_processed}", file=sys.stderr)

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

                    # Check for hard braking incident (deceleration > 0.8g)
                    # Convert to g-forces for incident detector
                    accel_g = motion_magnitude / 9.81
                    self.incident_detector.add_accelerometer_sample(accel_g)
                    self.incident_detector.check_hard_braking(accel_g)

                    # Check for impact incident (acceleration > 1.5g)
                    self.incident_detector.check_impact(accel_g)

                    # PHASE 2: Package accel data with timestamp
                    timestamp = time.time() - self.start_time
                    accel_packet = {
                        'timestamp': timestamp,
                        'magnitude': motion_magnitude
                    }

                    # PHASE 2: Distribute to ALL filter queues (non-blocking)
                    try:
                        self.ekf_accel_queue.put_nowait(accel_packet)
                    except Full:
                        pass
                    except Exception as e:
                        print(f"[ACCEL_LOOP] Error distributing to EKF accel queue: {type(e).__name__}: {e}", file=sys.stderr)

                    try:
                        self.comp_accel_queue.put_nowait(accel_packet)
                    except Full:
                        pass
                    except Exception as e:
                        print(f"[ACCEL_LOOP] Error distributing to Complementary accel queue: {type(e).__name__}: {e}", file=sys.stderr)

                    try:
                        self.es_ekf_accel_queue.put_nowait(accel_packet)
                    except Full:
                        pass
                    except Exception as e:
                        print(f"[ACCEL_LOOP] Error distributing to ES-EKF accel queue: {type(e).__name__}: {e}", file=sys.stderr)

                    # Create placeholder accel sample (will be updated by filter threads)
                    with self._save_lock:
                        if self.accel_index < self.max_accel_samples:
                            self.accel_samples[self.accel_index] = (timestamp, motion_magnitude)
                            self.accel_index += 1
                        else:
                            print(f"⚠️ Accel buffer full ({self.max_accel_samples} samples), skipping", file=sys.stderr)
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

            # NOTE: Gyroscope data comes from accel daemon's shared gyro_queue
            # No separate health checks needed since gyro shares the accel subprocess
            # If accel is alive, gyro data is available when it arrives from hardware

    def _gyro_loop(self):
        """Process gyroscope samples and feed to EKF filter (if enabled)"""
        import sys
        samples_collected = 0
        print("[GYRO_LOOP] Started", file=sys.stderr)
        while not self.stop_event.is_set():
            # Skip if gyro not available
            if not self.gyro_daemon or not self.enable_gyro:
                time.sleep(0.5)
                continue

            gyro_data = self.gyro_daemon.get_data(timeout=0.1)

            if gyro_data:
                samples_collected += 1
                if samples_collected <= 5 or samples_collected % 100 == 0:
                    print(f"[GYRO_LOOP] Processed sample #{samples_collected}", file=sys.stderr)
                try:
                    # Extract gyroscope angular velocities (rad/s)
                    # Data now comes pre-extracted as {'x': ..., 'y': ..., 'z': ...}
                    gyro_x = float(gyro_data.get('x', 0))  # rad/s
                    gyro_y = float(gyro_data.get('y', 0))  # rad/s
                    gyro_z = float(gyro_data.get('z', 0))  # rad/s

                    magnitude = (gyro_x**2 + gyro_y**2 + gyro_z**2) ** 0.5

                    # Add gyro sample for incident context (30s before/after events)
                    self.incident_detector.add_gyroscope_sample(magnitude)

                    # Check for swerving incident (yaw rotation rate)
                    # SMART DETECTION: Distinguish vehicle swerving from phone movement
                    # Approach: Only flag swerving if sustained rotation (multi-sample coherence)
                    # Single-frame spikes are filtered out (phone flips/slides)
                    #
                    # Swerving = vehicle turning (requires sustained yaw > 60°/sec)
                    # Phone flip = momentary high rotation (single/few samples)
                    #
                    # Additional context:
                    # 1. Vehicle moving > 2 m/s (7.2 km/h) via GPS
                    # 2. Consistent heading from EKF (no wild jumps = no reorientation)
                    if self.gps_index > 0 and self.ekf.enable_gyro:
                        latest_gps = self.gps_samples[self.gps_index - 1]
                        vehicle_speed = float(latest_gps['speed'])

                        # Only detect swerving during active vehicle motion
                        # Threshold: 2 m/s prevents stationary phone movement triggers
                        if vehicle_speed > 2.0:
                            # Use higher threshold for individual samples (filter phone flips)
                            # Real swerving happens over 200+ ms, phone flips are milliseconds
                            # 60°/sec physical swerving = higher yaw sustained over time
                            # Quick phone rotation = brief spike, filtered by cooldown + threshold
                            if abs(gyro_z) > 1.047:  # Only check if yaw exceeds swerving threshold
                                self.incident_detector.check_swerving(abs(gyro_z))

                    # PHASE 2: Package gyro data
                    timestamp = time.time() - self.start_time
                    gyro_packet = {
                        'timestamp': timestamp,
                        'gyro_x': gyro_x,
                        'gyro_y': gyro_y,
                        'gyro_z': gyro_z,
                        'magnitude': magnitude
                    }

                    # PHASE 2: Distribute to filter queues (only EKF and ES-EKF support gyro)
                    try:
                        self.ekf_gyro_queue.put_nowait(gyro_packet)
                    except Full:
                        pass
                    except Exception as e:
                        print(f"[GYRO_LOOP] Error distributing to EKF gyro queue: {type(e).__name__}: {e}", file=sys.stderr)

                    try:
                        self.es_ekf_gyro_queue.put_nowait(gyro_packet)
                    except Full:
                        pass
                    except Exception as e:
                        print(f"[GYRO_LOOP] Error distributing to ES-EKF gyro queue: {type(e).__name__}: {e}", file=sys.stderr)

                    # Create placeholder gyro sample
                    with self._save_lock:
                        if self.gyro_index < self.max_gyro_samples:
                            self.gyro_samples[self.gyro_index] = (timestamp, magnitude)
                            self.gyro_index += 1
                        else:
                            print(f"⚠️ Gyro buffer full ({self.max_gyro_samples} samples), skipping", file=sys.stderr)
                except Exception as e:
                    print(f"ERROR in gyro loop at {time.time() - self.start_time:.2f}s: {e}", file=sys.stderr)
                    import traceback
                    traceback.print_exc()

        print(f"[GYRO_LOOP] Exited after processing {samples_collected} samples", file=sys.stderr)

    def _ekf_filter_thread(self):
        """PHASE 3: Independent EKF filter processing thread"""
        print("[EKF_THREAD] Started", file=sys.stderr)
        sys.stderr.flush()
        samples_processed = {'accel': 0, 'gps': 0, 'gyro': 0}

        while not self.stop_event.is_set():
            try:
                # Process accel (high frequency - non-blocking check)
                try:
                    accel_packet = self.ekf_accel_queue.get(timeout=0.01)
                    v, d = self.ekf.update_accelerometer(accel_packet['magnitude'])
                    samples_processed['accel'] += 1

                    # NOTE: Filter results stored in filter object state, not written back to samples
                    # Numpy arrays have fixed schemas - can't dynamically add fields
                except Empty:
                    pass

                # Process GPS (low frequency)
                try:
                    gps_packet = self.ekf_gps_queue.get(timeout=0.01)
                    v, d = self.ekf.update_gps(
                        gps_packet['latitude'], gps_packet['longitude'],
                        gps_packet['speed'], gps_packet['accuracy']
                    )
                    samples_processed['gps'] += 1

                    # Store trajectory
                    if hasattr(self.ekf, 'get_position'):
                        try:
                            lat, lon, unc = self.ekf.get_position()
                            with self._save_lock:
                                self._record_trajectory_point(
                                    'ekf',
                                    gps_packet['timestamp'],
                                    lat,
                                    lon,
                                    v,
                                    unc
                                )
                                self._record_covariance_snapshot(gps_packet['timestamp'])
                        except:
                            pass

                    # NOTE: Filter results stored in trajectory and filter object state
                    # Numpy arrays have fixed schemas - can't dynamically add fields

                except Empty:
                    pass

                # Process gyro (if enabled)
                if self.enable_gyro:
                    try:
                        gyro_packet = self.ekf_gyro_queue.get(timeout=0.01)
                        v, d = self.ekf.update_gyroscope(
                            gyro_packet['gyro_x'], gyro_packet['gyro_y'], gyro_packet['gyro_z']
                        )
                        samples_processed['gyro'] += 1

                        # NOTE: Filter results stored in filter object state
                        # Numpy arrays have fixed schemas - can't dynamically add fields

                        # Update metrics
                        if self.metrics:
                            ekf_state = self.ekf.get_state()
                            gps_heading = None
                            # NOTE: GPS dtype doesn't include bearing/heading fields - skip for now

                            accel_magnitude = 0
                            with self._save_lock:
                                if self.accel_index > 0:
                                    accel_magnitude = float(self.accel_samples[self.accel_index - 1]['magnitude'])

                            self.metrics.update(
                                ekf_state=ekf_state,
                                gyro_measurement=[gyro_packet['gyro_x'], gyro_packet['gyro_y'], gyro_packet['gyro_z']],
                                gps_heading=gps_heading,
                                accel_magnitude=accel_magnitude
                            )
                    except Empty:
                        pass

                # Brief sleep to avoid CPU spinning
                time.sleep(0.001)

            except Exception as e:
                print(f"ERROR in EKF filter thread: {e}", file=sys.stderr)
                import traceback
                traceback.print_exc()
                time.sleep(0.1)  # Backoff on error

        print(f"[EKF_THREAD] Exited after processing {samples_processed}", file=sys.stderr)

    def _complementary_filter_thread(self):
        """PHASE 3: Independent Complementary filter processing thread"""
        print("[COMP_THREAD] Started", file=sys.stderr)
        sys.stderr.flush()
        samples_processed = {'accel': 0, 'gps': 0}

        while not self.stop_event.is_set():
            try:
                # Process accel
                try:
                    accel_packet = self.comp_accel_queue.get(timeout=0.01)
                    v, d = self.complementary.update_accelerometer(accel_packet['magnitude'])
                    samples_processed['accel'] += 1

                    # NOTE: Filter results stored in filter object state, not written back to samples
                    # Numpy arrays have fixed schemas - can't dynamically add fields
                except Empty:
                    pass

                # Process GPS
                try:
                    gps_packet = self.comp_gps_queue.get(timeout=0.01)
                    v, d = self.complementary.update_gps(
                        gps_packet['latitude'], gps_packet['longitude'],
                        gps_packet['speed'], gps_packet['accuracy']
                    )
                    samples_processed['gps'] += 1

                    # Store trajectory
                    if hasattr(self.complementary, 'get_position'):
                        try:
                            lat, lon, unc = self.complementary.get_position()
                            with self._save_lock:
                                self._record_trajectory_point(
                                    'complementary',
                                    gps_packet['timestamp'],
                                    lat,
                                    lon,
                                    v,
                                    unc
                                )
                        except:
                            pass

                    # NOTE: Filter results stored in trajectory and filter object state
                    # Numpy arrays have fixed schemas - can't dynamically add fields

                except Empty:
                    pass

                time.sleep(0.001)

            except Exception as e:
                print(f"ERROR in Complementary filter thread: {e}", file=sys.stderr)
                import traceback
                traceback.print_exc()
                time.sleep(0.1)

        print(f"[COMP_THREAD] Exited after processing {samples_processed}", file=sys.stderr)

    def _es_ekf_filter_thread(self):
        """PHASE 3: Independent ES-EKF filter processing thread (EXPERIMENTAL - may hang)"""
        print("[ES_EKF_THREAD] Started", file=sys.stderr)
        sys.stderr.flush()
        samples_processed = {'accel': 0, 'gps': 0, 'gyro': 0}
        consecutive_failures = 0
        predict_interval = getattr(self.es_ekf, 'dt', 0.02)
        last_predict = time.time()

        while not self.stop_event.is_set():
            try:
                now = time.time()
                # Run prediction steps to keep the dead-reckoning state moving between sensor updates
                while now - last_predict >= predict_interval:
                    self.es_ekf.predict()
                    last_predict += predict_interval
                synthetic_ts = (time.time() - self.start_time) if self.start_time else time.time()
                self._maybe_emit_es_dead_reckoning(synthetic_ts)

                # MEMORY GUARD: Skip processing if paused due to memory pressure
                if self.es_ekf_paused:
                    time.sleep(0.1)  # Sleep while paused
                    # Drain queues to prevent backup during pause
                    try:
                        while not self.es_ekf_accel_queue.empty():
                            self.es_ekf_accel_queue.get_nowait()
                        while not self.es_ekf_gps_queue.empty():
                            self.es_ekf_gps_queue.get_nowait()
                        while not self.es_ekf_gyro_queue.empty():
                            self.es_ekf_gyro_queue.get_nowait()
                    except:
                        pass
                    continue

                # Process accel
                try:
                    accel_packet = self.es_ekf_accel_queue.get(timeout=0.01)
                    v, d = self.es_ekf.update_accelerometer(accel_packet['magnitude'])
                    samples_processed['accel'] += 1
                    consecutive_failures = 0

                    # NOTE: Filter results stored in filter object state
                    # Numpy arrays have fixed schemas - can't dynamically add fields

                except Empty:
                    pass
                except Exception as e:
                    # ES-EKF may hang - log but continue
                    consecutive_failures += 1
                    if consecutive_failures == 1:
                        print(f"⚠️  ES-EKF accel update failed at sample #{samples_processed['accel']}: {e}", file=sys.stderr)

                    # After 10 consecutive failures, drain queue to prevent backup
                    if consecutive_failures > 10:
                        try:
                            drained = 0
                            while True:
                                self.es_ekf_accel_queue.get_nowait()
                                drained += 1
                        except Empty:
                            print(f"  → Drained {drained} backed-up accel samples", file=sys.stderr)
                            consecutive_failures = 0

                # Process GPS
                try:
                    gps_packet = self.es_ekf_gps_queue.get(timeout=0.01)
                    v, d = self.es_ekf.update_gps(
                        gps_packet['latitude'], gps_packet['longitude'],
                        gps_packet['speed'], gps_packet['accuracy']
                    )
                    self._update_motion_profile(gps_packet.get('speed'))
                    samples_processed['gps'] += 1

                    # Store trajectory
                    try:
                        lat, lon, unc = self.es_ekf.get_position()
                        with self._save_lock:
                            self._record_trajectory_point(
                                'es_ekf',
                                gps_packet['timestamp'],
                                lat,
                                lon,
                                v,
                                unc
                            )
                        self._last_es_ekf_gps_ts = gps_packet['timestamp']
                    except:
                        pass

                    # NOTE: Filter results stored in trajectory and filter object state
                    # Numpy arrays have fixed schemas - can't dynamically add fields

                except Empty:
                    pass
                except Exception as e:
                    print(f"⚠️  ES-EKF GPS update failed: {e}", file=sys.stderr)

                # Process gyro
                if self.enable_gyro:
                    try:
                        gyro_packet = self.es_ekf_gyro_queue.get(timeout=0.01)
                        v, d = self.es_ekf.update_gyroscope(
                            gyro_packet['gyro_x'], gyro_packet['gyro_y'], gyro_packet['gyro_z']
                        )
                        samples_processed['gyro'] += 1
                    except Empty:
                        pass
                    except Exception as e:
                        print(f"⚠️  ES-EKF gyro update failed: {e}", file=sys.stderr)

                time.sleep(0.001)

            except Exception as e:
                print(f"ERROR in ES-EKF filter thread (continuing): {e}", file=sys.stderr)
                time.sleep(0.1)

        print(f"[ES_EKF_THREAD] Exited after processing {samples_processed}", file=sys.stderr)

    def _display_loop(self):
        """Display metrics every second, log status every 30 seconds"""
        last_display = 0
        last_status_log = 0

        while not self.stop_event.is_set():
            try:
                now = time.time()

                # Log status every 30 seconds (to stderr)
                if now - last_status_log > 30.0:
                    last_status_log = now
                    try:
                        self._log_status()
                    except Exception as e:
                        print(f"⚠️  Warning: Status logging failed: {e}", file=sys.stderr)

                # Display metrics every second
                if now - last_display > 1.0:
                    last_display = now
                    try:
                        self._display_metrics()
                    except Exception as e:
                        print(f"⚠️  Warning: Metrics display failed: {e}", file=sys.stderr)

                # PHASE 6: Check queue health
                queue_depths = {
                    'ekf_accel': self.ekf_accel_queue.qsize(),
                    'ekf_gps': self.ekf_gps_queue.qsize(),
                    'comp_accel': self.comp_accel_queue.qsize(),
                    'es_ekf_accel': self.es_ekf_accel_queue.qsize()
                }

                for name, depth in queue_depths.items():
                    if depth > 400:  # 80% full warning
                        print(f"⚠️  Queue {name} backing up: {depth} items", file=sys.stderr)

                # Update live status file every 2 seconds for dashboard
                if now - self.last_status_update > 2.0:
                    self.last_status_update = now
                    try:
                        self._update_live_status()
                    except Exception as e:
                        # Silently skip status file updates if they fail (non-critical)
                        pass

                time.sleep(0.1)
            except Exception as e:
                print(f"⚠️  Critical error in display loop: {e}", file=sys.stderr)
                # Continue display loop even on critical errors
                time.sleep(0.5)

    def _restart_accel_daemon(self):
        """Attempt to restart the accelerometer daemon (thread-safe with zombie cleanup)"""
        # LOCK: Prevent concurrent restart attempts from health monitor + status logger
        with self._accel_restart_lock:
            # DOUBLE-CHECK: Another thread may have already restarted AND data is flowing
            if self.accel_daemon and self.accel_daemon.is_alive():
                # VALIDATE: Process alive doesn't guarantee data flow - check queue
                test_data = self.accel_daemon.get_data(timeout=2.0)
                if test_data:
                    print(f"  → Accel already alive and producing data (concurrent restart won)", file=sys.stderr)
                    return True
                else:
                    print(f"  → Accel process alive but NOT producing data, forcing restart", file=sys.stderr)
                    # Fall through to perform actual restart

            print(f"\n🔄 Attempting to restart accelerometer daemon (attempt {self.restart_counts['accel'] + 1}/{self.max_restart_attempts})...", file=sys.stderr)

            # STEP 1: GRACEFUL STOP (terminate subprocess first)
            try:
                if self.accel_daemon:
                    self.accel_daemon.stop()  # Sends SIGTERM
            except Exception as e:
                print(f"  Warning stopping daemon: {e}", file=sys.stderr)

            # STEP 2: AGGRESSIVE KILL + ZOMBIE REAPING
            try:
                # Kill all termux-sensor processes
                subprocess.run(['pkill', '-9', 'termux-sensor'],
                              capture_output=True, timeout=2)

                # Kill termux-api backend (Android sensor service)
                # CRITICAL: Use specific pattern to avoid killing GPS backend
                subprocess.run(['pkill', '-9', '-f', 'termux-api Sensor'],
                              capture_output=True, timeout=2)

                # CRITICAL: WAIT FOR ZOMBIE REAPING (poll until processes gone)
                max_wait = 5.0  # seconds
                start_wait = time.time()
                while time.time() - start_wait < max_wait:
                    result = subprocess.run(['pgrep', '-x', 'termux-sensor'],
                                           capture_output=True, timeout=1)
                    if result.returncode != 0:  # No processes found
                        break
                    time.sleep(0.2)  # Poll every 200ms

                # VALIDATE cleanup succeeded
                result = subprocess.run(['pgrep', '-x', 'termux-sensor'],
                                       capture_output=True, timeout=1)
                if result.returncode == 0:
                    print(f"  ⚠️ WARNING: termux-sensor processes still alive after cleanup",
                          file=sys.stderr)
                    time.sleep(2)  # Extra wait

            except Exception as e:
                print(f"  Warning during process cleanup: {e}", file=sys.stderr)

            # STEP 3: CREATE NEW DAEMON (only after validated cleanup)
            try:
                self.accel_daemon = PersistentAccelDaemon(delay_ms=20)
            except Exception as e:
                print(f"  ✗ Failed to create new accelerometer daemon: {e}", file=sys.stderr)
                return False

            # STEP 4: EXTENDED COOLDOWN (Android sensor backend re-init)
            time.sleep(self.restart_cooldown + 2)  # 12 seconds total

            # STEP 5: START + VALIDATE (with retry)
            if self.accel_daemon.start():
                # EXTENDED timeout for post-crash recovery
                validation_timeout = 30.0  # Increased from 15s
                test_data = self.accel_daemon.get_data(timeout=validation_timeout)

                if test_data:
                    print(f"  ✓ Accelerometer daemon restarted successfully", file=sys.stderr)
                    self.restart_counts['accel'] += 1

                    # CRITICAL: Restart gyro daemon (shares accel's IMU stream)
                    if self.enable_gyro and self.gyro_daemon:
                        print(f"  ✓ Accel restarted, resuming data collection", file=sys.stderr)
                        self._restart_gyro_after_accel()

                    return True
                else:
                    # RETRY ONCE (backend may still be initializing)
                    print(f"  → No data after {validation_timeout}s, retrying...",
                          file=sys.stderr)
                    time.sleep(5)
                    test_data = self.accel_daemon.get_data(timeout=10.0)
                    if test_data:
                        print(f"  ✓ Validation succeeded on retry", file=sys.stderr)
                        self.restart_counts['accel'] += 1

                        # CRITICAL: Restart gyro daemon (shares accel's IMU stream)
                        if self.enable_gyro and self.gyro_daemon:
                            print(f"  ✓ Accel restarted, resuming data collection", file=sys.stderr)
                            self._restart_gyro_after_accel()

                        return True

                    print(f"  ✗ Accel daemon unresponsive after retry", file=sys.stderr)
                    return False
            else:
                print(f"  ✗ Failed to start accelerometer daemon process", file=sys.stderr)
                return False

    def _restart_gyro_after_accel(self):
        """Restart gyro daemon after accel restart (gyro shares accel's IMU stream)"""
        try:
            print(f"  🔄 Restarting gyro daemon (coupled to accel daemon)...", file=sys.stderr)

            # STEP 1: Stop old gyro daemon
            if self.gyro_daemon:
                try:
                    self.gyro_daemon.stop()
                except Exception as e:
                    print(f"  → Warning stopping gyro daemon: {e}", file=sys.stderr)

            # STEP 2: Create NEW gyro daemon with NEW accel_daemon reference
            # CRITICAL: Gyro must reference the NEW accel_daemon (LSM6DSO paired sensors)
            self.gyro_daemon = PersistentGyroDaemon(accel_daemon=self.accel_daemon, delay_ms=20)

            # STEP 3: Start new gyro daemon
            if self.gyro_daemon.start():
                print(f"  ✓ Gyro daemon restarted (sharing new accel stream)", file=sys.stderr)
                return True
            else:
                print(f"  ⚠️ WARNING: Gyro daemon failed to restart", file=sys.stderr)
                print(f"  → Continuing without gyroscope", file=sys.stderr)
                self.gyro_daemon = None
                self.enable_gyro = False
                return False

        except Exception as e:
            print(f"  ✗ Error restarting gyro daemon: {e}", file=sys.stderr)
            self.gyro_daemon = None
            self.enable_gyro = False
            return False

    def _restart_gps_daemon(self):
        """Attempt to restart the GPS daemon (thread-safe with zombie cleanup)"""
        # LOCK: Prevent concurrent restart attempts
        with self._gps_restart_lock:
            # DOUBLE-CHECK: Another thread may have already restarted
            if self.gps_daemon and self.gps_daemon.is_alive():
                print(f"  → GPS already alive (concurrent restart won)", file=sys.stderr)
                return True

            print(f"\n🔄 Attempting to restart GPS daemon (attempt {self.restart_counts['gps'] + 1}/{self.max_restart_attempts})...", file=sys.stderr)

            # STEP 1: GRACEFUL STOP
            try:
                if self.gps_daemon:
                    self.gps_daemon.stop()
                    time.sleep(1)
            except Exception as e:
                print(f"  Warning during GPS daemon stop: {e}", file=sys.stderr)

            # STEP 2: AGGRESSIVE KILL + ZOMBIE REAPING
            try:
                # Kill all termux-location processes (GPS wrapper)
                subprocess.run(['pkill', '-9', 'termux-location'],
                              capture_output=True, timeout=2)

                # Kill termux-api backend
                subprocess.run(['pkill', '-9', 'termux-api'],
                              capture_output=True, timeout=2)

                # CRITICAL: WAIT FOR ZOMBIE REAPING (check termux-location, not python3)
                max_wait = 5.0
                start_wait = time.time()
                while time.time() - start_wait < max_wait:
                    result = subprocess.run(['pgrep', '-x', 'termux-location'],
                                           capture_output=True, timeout=1)
                    if result.returncode != 0:  # No termux-location processes found
                        break
                    time.sleep(0.2)  # Poll every 200ms

                # Validate cleanup succeeded
                result = subprocess.run(['pgrep', '-x', 'termux-location'],
                                       capture_output=True, timeout=1)
                process_count = len(result.stdout.strip().split('\n')) if result.stdout else 0
                if process_count > 0:
                    print(f"  ⚠️ WARNING: {process_count} termux-location processes still alive after cleanup",
                          file=sys.stderr)

                # Extended wait for Android cleanup
                time.sleep(2)

            except Exception as e:
                print(f"  Warning during process cleanup: {e}", file=sys.stderr)

            # STEP 3: DO NOT CREATE NEW DAEMON, RESTART EXISTING ONE
            # The existing daemon object (self.gps_daemon) is reused.
            # After the process cleanup, we will call start() on it again.
            pass

            # STEP 4: EXTENDED COOLDOWN
            time.sleep(self.restart_cooldown + 2)

            # STEP 5: START + VALIDATE
            if self.gps_daemon.start():
                print(f"  ✓ GPS daemon process started, waiting for first fix...", file=sys.stderr)

                # Validate GPS thread is running (not data collection - that may take time)
                # CHANGED: Don't stop daemon if no fix - GPS may need time to acquire signal
                # Instead, just validate the thread started and let it keep trying
                validation_timeout = 10  # seconds to wait for thread startup
                validation_start = time.time()
                fix_received = False

                while time.time() - validation_start < validation_timeout:
                    gps_data = self.gps_daemon.get_data(timeout=1.0)
                    if gps_data:
                        print(f"  ✓ GPS restart validated: {gps_data['latitude']:.4f}, {gps_data['longitude']:.4f}", file=sys.stderr)
                        fix_received = True
                        break
                    # Brief wait before retrying
                    time.sleep(0.5)

                if not fix_received:
                    print(f"  ⚠️ GPS restart: No fix yet (will keep trying in background)", file=sys.stderr)
                    # DON'T stop daemon - let it keep trying to acquire signal
                    # self.gps_daemon.stop()  # REMOVED
                    # return False  # REMOVED

                print(f"  ✓ GPS daemon restarted (fix_received={fix_received})", file=sys.stderr)
                self.restart_counts['gps'] += 1
                return True
            else:
                print(f"  ✗ Failed to restart GPS daemon", file=sys.stderr)
                return False

    def _log_status(self):
        """Log status update to stderr (won't clutter display)"""
        try:
            elapsed = time.time() - self.start_time
            mins = int(elapsed // 60)
            secs = int(elapsed % 60)

            # Memory
            try:
                mem_info = self.process.memory_info()
                mem_mb = mem_info.rss / 1024 / 1024
                self.peak_memory = max(self.peak_memory, mem_mb)

                # MEMORY GUARD: Prevent Android LMK from killing process at ~100 MB
                # Pause ES-EKF (most memory-intensive filter) if memory > 95 MB
                # Removed auto-pause: ES-EKF now remains active even under memory pressure.

            except Exception as e:
                mem_mb = self.peak_memory  # Use last known peak if current fails
                print(f"  ⚠️  Warning: Failed to get memory info: {e}", file=sys.stderr)

            # Get heap size from tracemalloc
            try:
                current_heap, peak_heap = tracemalloc.get_traced_memory()
                heap_mb = current_heap / (1024 * 1024)
                peak_heap_mb = peak_heap / (1024 * 1024)
            except Exception:
                heap_mb = 0
                peak_heap_mb = 0

            # Sample counts
            try:
                gps_count = len(self.gps_samples)
                accel_count = len(self.accel_samples)
                gyro_count = len(self.gyro_samples)
            except Exception as e:
                print(f"  ⚠️  Warning: Failed to count samples: {e}", file=sys.stderr)
                gps_count = accel_count = gyro_count = 0

            with self._save_lock:
                ekf_traj_count = self.trajectory_total_counts.get('ekf', 0)
                es_traj_count = self.trajectory_total_counts.get('es_ekf', 0)
                comp_traj_count = self.trajectory_total_counts.get('complementary', 0)
                cov_count = self.covariance_total_count

            # ⚠️ CRITICAL: Check daemon health every 30 seconds
            try:
                accel_status = self.accel_daemon.get_status() if self.accel_daemon else "DISABLED"
                gps_status = self.gps_daemon.get_status() if self.gps_daemon else "DISABLED"
            except Exception as e:
                print(f"  ⚠️  Warning: Failed to get daemon status: {e}", file=sys.stderr)
                accel_status = "UNKNOWN"
                gps_status = "UNKNOWN"

            status_msg = (
                f"[{mins:02d}:{secs:02d}] STATUS: RSS={mem_mb:.1f}MB (peak={self.peak_memory:.1f}MB) "
                f"Heap={heap_mb:.1f}MB (peak={peak_heap_mb:.1f}MB) | "
                f"GPS={gps_count:4d} ({gps_status}) | Accel={accel_count:5d} ({accel_status})"
            )

            if self.enable_gyro:
                status_msg += f" | Gyro={gyro_count:5d}"

            status_msg += (
                f" | Traj(E/ES/Comp)={ekf_traj_count}/{es_traj_count}/{comp_traj_count}"
                f" | Cov={cov_count}"
            )

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
                try:
                    self.metrics.print_dashboard(interval=30)
                except Exception as e:
                    print(f"  ⚠️  Warning: Failed to print metrics: {e}", file=sys.stderr)
        except Exception as e:
            print(f"⚠️  Critical error in _log_status: {e}", file=sys.stderr)
            # Continue anyway - status logging is non-critical

    def _display_metrics(self):
        """Show 3-column filter comparison: EKF, ES-EKF, Complementary"""
        try:
            if not self.gps_samples and not self.accel_samples:
                return

            # Get latest state from all three filters (with lock to prevent race condition)
            try:
                with self.state_lock:
                    ekf_state = self.ekf.get_state() or {}
                    comp_state = self.complementary.get_state() or {}
                    # FIXED: ES-EKF now uses RLock (re-entrant) instead of Lock - deadlock resolved
                    es_ekf_state = (self.es_ekf.get_state() or {}) if hasattr(self, 'es_ekf') else {}
            except Exception as e:
                # Skip display if filter states are inaccessible
                return

            elapsed = time.time() - self.start_time
            mins = int(elapsed // 60)
            secs = int(elapsed % 60)

            # Get latest sensor data
            latest_accel = self.accel_samples[-1] if self.accel_samples else None
            latest_gps = self.gps_samples[self.gps_index - 1] if self.gps_samples else None

            print(f"\n[{mins:02d}:{secs:02d}] FILTER COMPARISON (3-way)")
            print("-" * 130)

            # Header
            print(f"{'METRIC':<25} | {'EKF 13D':^20} | {'ES-EKF 8D (DR)':^20} | {'COMPLEMENTARY':^20}")
            print("-" * 130)

            # Velocity
            ekf_vel = ekf_state.get('velocity', 0.0)
            comp_vel = comp_state.get('velocity', 0.0)
            es_ekf_vel = es_ekf_state.get('velocity', 0.0) if es_ekf_state else 0.0
            print(f"{'Velocity (m/s)':<25} | {ekf_vel:>8.3f} m/s       | {es_ekf_vel:>8.3f} m/s        | {comp_vel:>8.3f} m/s      ")

            # Distance
            ekf_dist = ekf_state.get('distance', 0.0)
            comp_dist = comp_state.get('distance', 0.0)
            es_ekf_dist = es_ekf_state.get('distance', 0.0) if es_ekf_state else 0.0
            print(f"{'Distance (m)':<25} | {ekf_dist:>8.2f} m         | {es_ekf_dist:>8.2f} m          | {comp_dist:>8.2f} m       ")

            # Acceleration magnitude
            ekf_accel = ekf_state.get('accel_magnitude', 0.0)
            comp_accel = comp_state.get('accel_magnitude', 0.0)
            es_ekf_accel = es_ekf_state.get('accel_magnitude', 0.0) if es_ekf_state else 0.0
            print(f"{'Accel Magnitude (m/s²)':<25} | {ekf_accel:>8.3f} m/s²      | {es_ekf_accel:>8.3f} m/s²       | {comp_accel:>8.3f} m/s²    ")

            # Status
            ekf_status = "MOVING" if not ekf_state.get('is_stationary', False) else "STATIONARY"
            comp_status = "MOVING" if not comp_state.get('is_stationary', False) else "STATIONARY"
            es_ekf_status = "MOVING" if es_ekf_state and not es_ekf_state.get('is_stationary', False) else ("STATIONARY" if es_ekf_state else "N/A")
            print(f"{'Status':<25} | {ekf_status:^20} | {es_ekf_status:^20} | {comp_status:^20}")
        except Exception as e:
            # Non-critical display failure, skip metrics
            pass

        # Sensor info
        print("-" * 130)
        # FIX 6: Show total GPS fixes (cumulative), not just recent window
        # REAL-TIME DISPLAY: Show GPS daemon status alongside counts
        gps_status = self.gps_daemon.get_status() if self.gps_daemon else "DISABLED"
        accel_status = self.accel_daemon.get_status()
        sensor_info = f"GPS: {self.total_gps_fixes} fixes ({gps_status}) | Accel: {len(self.accel_samples)} samples ({accel_status})"
        if self.enable_gyro:
            sensor_info += f" | Gyro: {len(self.gyro_samples)} samples"
        print(sensor_info)

    def _update_live_status(self):
        """Update live status file for dashboard monitoring (lightweight, ~200 bytes)"""
        try:
            # Get latest state
            ekf_state = self.ekf.get_state()
            now_ts = time.time()
            if ekf_state:
                gps_updates = ekf_state.get('gps_updates', 0)
                if gps_updates > self._last_gps_fix_count:
                    self._last_gps_fix_count = gps_updates
                    self._last_gps_fix_time = now_ts
                    self._gps_cadence_warning_logged = False
                elif (now_ts - self._last_gps_fix_time) > 30 and not self._gps_cadence_warning_logged:
                    print("[GPS] Warning: No GPS fixes for 30+ seconds. Bringing Termux to the foreground may be required.", file=sys.stderr)
                    self._gps_cadence_warning_logged = True

            # Get latest GPS if available
            latest_gps = None
            if self.gps_index > 0:
                last = self.gps_samples[self.gps_index - 1]
                latest_gps = {
                    'lat': float(last['latitude']),
                    'lon': float(last['longitude']),
                    'accuracy': float(last['accuracy'])
                }

            # Get incident count
            incidents_count = len(self.incident_detector.get_recent_incidents()) if hasattr(self.incident_detector, 'get_recent_incidents') else 0

            # Get memory usage
            mem_info = self.process.memory_info()
            mem_mb = mem_info.rss / 1024 / 1024

            # Session ID from timestamp
            if hasattr(self, 'start_time') and isinstance(self.start_time, float):
                session_id = f"comparison_{datetime.fromtimestamp(self.start_time).strftime('%Y%m%d_%H%M%S')}"
            else:
                session_id = f"comparison_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

            with self._save_lock:
                traj_counts = {
                    'ekf': self.trajectory_total_counts.get('ekf', 0),
                    'es_ekf': self.trajectory_total_counts.get('es_ekf', 0),
                    'complementary': self.trajectory_total_counts.get('complementary', 0)
                }
                cov_snapshot = self.covariance_total_count

            status_data = {
                'session_id': session_id,
                'status': 'ACTIVE',
                'elapsed_seconds': int(time.time() - self.start_time),
                'last_update': now_ts,
                'gps_fixes': self.total_gps_fixes,
                'accel_samples': len(self.accel_samples),
                'gyro_samples': len(self.gyro_samples) if self.enable_gyro else 0,
                'current_velocity': round(ekf_state['velocity'], 2),
                'current_heading': round(ekf_state.get('heading_deg', 0), 1) if self.enable_gyro else None,
                'total_distance': round(ekf_state['distance'], 1),
                'latest_gps': latest_gps,
                'incidents_count': incidents_count,
                'memory_mb': round(mem_mb, 1),
                'filter_type': 'EKF' if self.enable_gyro else 'EKF+Complementary',
                'gps_first_fix_latency': round(self.gps_first_fix_latency, 1) if self.gps_first_fix_latency else None,
                'trajectory_samples_retained': traj_counts,
                'covariance_samples_retained': cov_snapshot
            }

            # Atomic write with temp file
            temp_file = f"{self.status_file}.tmp"
            with open(temp_file, 'w') as f:
                json.dump(status_data, f, separators=(',', ':'))
            os.rename(temp_file, self.status_file)

        except Exception as e:
            # Log status update failure for debugging
            print(f"[DEBUG] Status file update failed: {e}", file=sys.stderr)

    def stop(self):
        self.stop_event.set()
        self.accel_daemon.stop()
        self.gps_daemon.stop()

        # Clean up live status file
        try:
            if os.path.exists(self.status_file):
                os.remove(self.status_file)
        except FileNotFoundError:
            # Status file already deleted, no problem
            pass
        except Exception as e:
            # Log any other file operation errors but don't crash
            print(f"Warning: Failed to clean up status file: {type(e).__name__}: {e}", file=sys.stderr)

        # CRITICAL: Verify accelerometer data was collected
        # FIX 1: Check both accumulated_data and current buffers
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

    def _numpy_to_list(self, arr, count, dtype_name):
        """Convert numpy structured array to list of dicts for JSON serialization"""
        if count == 0:
            return []

        if dtype_name == 'gps':
            return [
                {
                    'timestamp': float(arr[i]['timestamp']),
                    'latitude': float(arr[i]['latitude']),
                    'longitude': float(arr[i]['longitude']),
                    'accuracy': float(arr[i]['accuracy']),
                    'speed': float(arr[i]['speed']),
                    'provider': str(arr[i]['provider'])
                }
                for i in range(count)
            ]
        elif dtype_name in ['accel', 'gyro']:
            return [
                {
                    'timestamp': float(arr[i]['timestamp']),
                    'magnitude': float(arr[i]['magnitude'])
                }
                for i in range(count)
            ]
        else:
            return []

    def _trajectory_buffer_to_list(self, buffer, count):
        return [
            {
                'timestamp': float(buffer[i]['timestamp']),
                'lat': float(buffer[i]['lat']),
                'lon': float(buffer[i]['lon']),
                'velocity': float(buffer[i]['velocity']),
                'uncertainty_m': float(buffer[i]['uncertainty'])
            }
            for i in range(count)
        ]

    def _covariance_buffer_to_list(self, buffer, count):
        return [
            {
                'timestamp': float(buffer[i]['timestamp']),
                'trace': float(buffer[i]['trace']),
                'diagonal': [
                    float(buffer[i]['p00']),
                    float(buffer[i]['p11']),
                    float(buffer[i]['p22']),
                    float(buffer[i]['p33']),
                    float(buffer[i]['p44']),
                    float(buffer[i]['p55'])
                ]
            }
            for i in range(count)
        ]

    def _init_sensor_cache(self):
        """Initialize SQLite cache used to store auto-saved sensor samples."""
        try:
            conn = sqlite3.connect(self._sensor_db_path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS gps_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL,
                    latitude REAL,
                    longitude REAL,
                    accuracy REAL,
                    speed REAL,
                    provider TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS accel_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL,
                    magnitude REAL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS gyro_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL,
                    magnitude REAL
                )
            """)
            conn.commit()
            self._sensor_db_conn = conn
        except Exception as e:
            print(f"⚠️ Warning: Failed to initialize sensor cache database ({e})", file=sys.stderr)
            self._sensor_db_conn = None

    def _persist_sensor_samples(self, sensor_type, samples):
        """Persist sensor samples to SQLite so they survive memory clears."""
        if not samples or not self._sensor_db_conn:
            return

        if sensor_type == 'gps':
            sql = """
                INSERT INTO gps_samples (timestamp, latitude, longitude, accuracy, speed, provider)
                VALUES (?, ?, ?, ?, ?, ?)
            """
            rows = [
                (
                    float(sample.get('timestamp', 0.0)),
                    float(sample.get('latitude', 0.0)),
                    float(sample.get('longitude', 0.0)),
                    float(sample.get('accuracy', 0.0)),
                    float(sample.get('speed', 0.0)),
                    str(sample.get('provider', 'gps'))
                )
                for sample in samples
            ]
        elif sensor_type == 'accel':
            sql = "INSERT INTO accel_samples (timestamp, magnitude) VALUES (?, ?)"
            rows = [
                (
                    float(sample.get('timestamp', 0.0)),
                    float(sample.get('magnitude', 0.0))
                )
                for sample in samples
            ]
        elif sensor_type == 'gyro':
            sql = "INSERT INTO gyro_samples (timestamp, magnitude) VALUES (?, ?)"
            rows = [
                (
                    float(sample.get('timestamp', 0.0)),
                    float(sample.get('magnitude', 0.0))
                )
                for sample in samples
            ]
        else:
            return

        try:
            with self._sensor_db_conn:
                self._sensor_db_conn.executemany(sql, rows)
        except Exception as e:
            print(f"⚠️ Warning: Failed to persist {sensor_type} samples to SQLite ({e})", file=sys.stderr)

    def _load_sensor_samples(self, sensor_type):
        """Load all persisted samples for a sensor type."""
        if not self._sensor_db_conn:
            return []

        if sensor_type == 'gps':
            sql = "SELECT timestamp, latitude, longitude, accuracy, speed, provider FROM gps_samples ORDER BY id"
            try:
                rows = self._sensor_db_conn.execute(sql).fetchall()
                return [
                    {
                        'timestamp': float(ts),
                        'latitude': float(lat),
                        'longitude': float(lon),
                        'accuracy': float(acc),
                        'speed': float(spd),
                        'provider': provider or 'gps'
                    }
                    for ts, lat, lon, acc, spd, provider in rows
                ]
            except Exception as e:
                print(f"⚠️ Warning: Failed to load GPS samples from SQLite ({e})", file=sys.stderr)
                return []
        elif sensor_type == 'accel':
            sql = "SELECT timestamp, magnitude FROM accel_samples ORDER BY id"
            try:
                rows = self._sensor_db_conn.execute(sql).fetchall()
                return [
                    {'timestamp': float(ts), 'magnitude': float(mag)}
                    for ts, mag in rows
                ]
            except Exception as e:
                print(f"⚠️ Warning: Failed to load accel samples from SQLite ({e})", file=sys.stderr)
                return []
        elif sensor_type == 'gyro':
            sql = "SELECT timestamp, magnitude FROM gyro_samples ORDER BY id"
            try:
                rows = self._sensor_db_conn.execute(sql).fetchall()
                return [
                    {'timestamp': float(ts), 'magnitude': float(mag)}
                    for ts, mag in rows
                ]
            except Exception as e:
                print(f"⚠️ Warning: Failed to load gyro samples from SQLite ({e})", file=sys.stderr)
                return []
        return []

    def _close_sensor_cache(self):
        """Close SQLite connection after final save."""
        if self._sensor_db_conn:
            try:
                self._sensor_db_conn.close()
            except Exception:
                pass
            self._sensor_db_conn = None

    def _read_chunk_file(self, path):
        try:
            with gzip.open(path, 'rt', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            return []
        except Exception as e:
            print(f"⚠️  Warning: Failed to read chunk file {path}: {e}", file=sys.stderr)
            return []

    def _flush_trajectory_buffer(self, filter_name, force=False):
        count = self.trajectory_indices[filter_name]
        if count == 0:
            return
        if not force and count < self.max_trajectory_points:
            return

        chunk_data = self._trajectory_buffer_to_list(self.trajectory_buffers[filter_name], count)
        chunk_file = os.path.join(
            self.buffer_chunk_dir,
            f"{filter_name}_traj_chunk_{len(self.trajectory_chunk_paths[filter_name]) + 1:04d}.json.gz"
        )
        try:
            with gzip.open(chunk_file, 'wt', encoding='utf-8') as f:
                json.dump(chunk_data, f, separators=(',', ':'))
            self.trajectory_chunk_paths[filter_name].append(chunk_file)
            self.trajectory_indices[filter_name] = 0
        except Exception as e:
            print(f"⚠️  Warning: Failed to flush {filter_name} trajectory buffer: {e}", file=sys.stderr)

    def _flush_covariance_buffer(self, force=False):
        count = self.covariance_index
        if count == 0:
            return
        if not force and count < self.max_covariance_snapshots:
            return

        chunk_data = self._covariance_buffer_to_list(self.covariance_buffer, count)
        chunk_file = os.path.join(
            self.buffer_chunk_dir,
            f"covariance_chunk_{len(self.covariance_chunk_paths) + 1:04d}.json.gz"
        )
        try:
            with gzip.open(chunk_file, 'wt', encoding='utf-8') as f:
                json.dump(chunk_data, f, separators=(',', ':'))
            self.covariance_chunk_paths.append(chunk_file)
            self.covariance_index = 0
        except Exception as e:
            print(f"⚠️  Warning: Failed to flush covariance buffer: {e}", file=sys.stderr)

    def _record_trajectory_point(self, filter_name, timestamp, lat, lon, velocity, uncertainty):
        if filter_name not in self.trajectory_buffers:
            return

        idx = self.trajectory_indices[filter_name]
        if idx >= self.max_trajectory_points:
            self._flush_trajectory_buffer(filter_name)
            idx = self.trajectory_indices[filter_name]

        self.trajectory_buffers[filter_name][idx] = (
            timestamp,
            lat,
            lon,
            velocity,
            uncertainty
        )
        self.trajectory_indices[filter_name] = idx + 1
        self.trajectory_total_counts[filter_name] += 1
        self._last_traj_emit[filter_name] = timestamp

    def _maybe_emit_es_dead_reckoning(self, timestamp):
        if self._last_es_ekf_gps_ts is None:
            return
        last_emit = self._last_traj_emit.get('es_ekf', 0.0)
        if timestamp - last_emit < self.dead_reckoning_emit_interval:
            return
        try:
            lat, lon, unc = self.es_ekf.get_position()
            state = self.es_ekf.get_state()
            velocity = state.get('velocity', 0.0)
            if velocity < 0.5:
                return
            with self._save_lock:
                self._record_trajectory_point('es_ekf', timestamp, lat, lon, velocity, unc)
                self._record_trajectory_point('es_ekf_dead_reckoning', timestamp, lat, lon, velocity, unc)
        except Exception:
            pass

    def _apply_motion_profile(self, profile):
        cfg = self.motion_profiles[profile]
        self.motion_profile = profile
        self.dead_reckoning_emit_interval = cfg['emit_interval']
        if hasattr(self, 'es_ekf') and hasattr(self.es_ekf, 'set_noise_profile'):
            self.es_ekf.set_noise_profile(
                gps_noise_std=cfg['gps_noise'],
                accel_noise_std=cfg['accel_noise'],
                gps_velocity_noise_std=cfg['gps_velocity_noise'],
            )
        print(f"[MotionProfile] Switched to {profile} mode (emit_interval={self.dead_reckoning_emit_interval}s)", file=sys.stderr)

    def _update_motion_profile(self, gps_speed):
        if gps_speed is None:
            return
        self._profile_speed_samples.append(gps_speed)
        if len(self._profile_speed_samples) < 5:
            return
        median_speed = statistics.median(self._profile_speed_samples)
        desired = 'pedestrian' if median_speed < self.pedestrian_speed_threshold else 'vehicle'
        if desired != self.motion_profile:
            self._apply_motion_profile(desired)

    def _record_covariance_snapshot(self, timestamp):
        if not hasattr(self.ekf, 'P'):
            return

        diag = np.diag(getattr(self.ekf, 'P'))
        diag_values = [float(diag[i]) if i < len(diag) else 0.0 for i in range(6)]
        trace_val = float(np.trace(getattr(self.ekf, 'P')))

        idx = self.covariance_index
        if idx >= self.max_covariance_snapshots:
            self._flush_covariance_buffer()
            idx = self.covariance_index

        self.covariance_buffer[idx] = (
            timestamp,
            trace_val,
            diag_values[0],
            diag_values[1],
            diag_values[2],
            diag_values[3],
            diag_values[4],
            diag_values[5]
        )
        self.covariance_index = idx + 1
        self.covariance_total_count += 1

    def _snapshot_trajectory_state(self, filter_name):
        with self._save_lock:
            chunk_paths = list(self.trajectory_chunk_paths.get(filter_name, []))
            count = self.trajectory_indices.get(filter_name, 0)
            buffer_snapshot = None
            if count:
                buffer_snapshot = np.copy(self.trajectory_buffers[filter_name][:count])
        return chunk_paths, buffer_snapshot

    def _snapshot_covariance_state(self):
        with self._save_lock:
            chunk_paths = list(self.covariance_chunk_paths)
            count = self.covariance_index
            buffer_snapshot = None
            if count:
                buffer_snapshot = np.copy(self.covariance_buffer[:count])
        return chunk_paths, buffer_snapshot

    def _assemble_trajectory_history(self, chunk_paths, buffer_snapshot):
        data = []
        for chunk_file in chunk_paths:
            data.extend(self._read_chunk_file(chunk_file))

        if buffer_snapshot is not None and len(buffer_snapshot) > 0:
            data.extend(self._trajectory_buffer_to_list(buffer_snapshot, len(buffer_snapshot)))
        return data

    def _assemble_covariance_history(self, chunk_paths, buffer_snapshot):
        data = []
        for chunk_file in chunk_paths:
            data.extend(self._read_chunk_file(chunk_file))

        if buffer_snapshot is not None and len(buffer_snapshot) > 0:
            data.extend(self._covariance_buffer_to_list(buffer_snapshot, len(buffer_snapshot)))
        return data

    def _get_full_trajectory(self, filter_name):
        chunk_paths, buffer_snapshot = self._snapshot_trajectory_state(filter_name)
        return self._assemble_trajectory_history(chunk_paths, buffer_snapshot)

    def _get_covariance_history(self):
        chunk_paths, buffer_snapshot = self._snapshot_covariance_state()
        return self._assemble_covariance_history(chunk_paths, buffer_snapshot)

    def _save_results(self, auto_save=False, clear_after_save=False):
        """Save results to JSON file (with auto-save and clear support)"""
        if hasattr(self, 'start_time') and isinstance(self.start_time, float):
            timestamp = datetime.fromtimestamp(self.start_time).strftime("%Y%m%d_%H%M%S")
        elif hasattr(self, 'start_time'):
            timestamp = self.start_time.strftime("%Y%m%d_%H%M%S")
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_filename = os.path.join(SESSIONS_DIR, f"comparison_{timestamp}")

        with self._save_lock:
            gps_samples_current = self._numpy_to_list(self.gps_samples, self.gps_index, 'gps')
            accel_samples_current = self._numpy_to_list(self.accel_samples, self.accel_index, 'accel')
            gyro_samples_current = (
                self._numpy_to_list(self.gyro_samples, self.gyro_index, 'gyro')
                if self.enable_gyro else []
            )

            trajectory_snapshots = {}
            for key in self.trajectory_buffers:
                chunk_paths = list(self.trajectory_chunk_paths[key])
                count = self.trajectory_indices[key]
                buffer_snapshot = None
                if count:
                    buffer_snapshot = np.copy(self.trajectory_buffers[key][:count])
                trajectory_snapshots[key] = (chunk_paths, buffer_snapshot)

            covariance_chunk_paths = list(self.covariance_chunk_paths)
            covariance_buffer_snapshot = (
                np.copy(self.covariance_buffer[:self.covariance_index])
                if self.covariance_index else None
            )

            trajectory_history = {
                key: self._assemble_trajectory_history(*trajectory_snapshots[key])
                for key in trajectory_snapshots
            }
            covariance_history = self._assemble_covariance_history(
                covariance_chunk_paths, covariance_buffer_snapshot
            )

            results = {
                'test_duration': self.duration_minutes,
                'actual_duration': time.time() - self.start_time,
                'peak_memory_mb': self.peak_memory,
                'auto_save': auto_save,
                'gps_available': self.total_gps_fixes > 0,  # Flag: Was GPS successfully collecting data?
                'gps_fixes_collected': self.total_gps_fixes,  # Total GPS fixes during test
                'gps_first_fix_latency_seconds': self.gps_first_fix_latency,  # Time to first GPS fix
                'gps_daemon_restart_count': self.restart_counts['gps'],  # How many times GPS restarted
                'gps_samples': gps_samples_current,  # Convert numpy to list of dicts
                'accel_samples': accel_samples_current,  # Convert numpy to list of dicts
                'gyro_samples': gyro_samples_current,  # Convert numpy to list of dicts
                'trajectories': trajectory_history,
                'covariance_snapshots': covariance_history
            }

            # Get final filter states (wrapped to avoid deadlock if filter threads still running)
            try:
                results['final_metrics'] = {
                    'ekf': self.ekf.get_state(),
                    'complementary': self.complementary.get_state()
                }
            except Exception as e:
                # Log error but don't crash - filters may still be processing
                print(f"Warning: Failed to get final filter states: {type(e).__name__}: {e}", file=sys.stderr)
                results['final_metrics'] = {'ekf': {}, 'complementary': {}}

            if auto_save:
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
                self._accumulated_data['gps_samples'].extend(gps_samples_current)
                self._accumulated_data['accel_samples'].extend(accel_samples_current)
                if self.enable_gyro:
                    self._accumulated_data['gyro_samples'].extend(gyro_samples_current)
                self._accumulated_data['autosave_count'] += 1

                # Write accumulated data with current metrics
                accumulated_results = {
                    'test_duration': self.duration_minutes,
                    'actual_duration': time.time() - self.start_time,
                    'peak_memory_mb': self.peak_memory,
                    'auto_save': auto_save,
                    'autosave_number': self._accumulated_data['autosave_count'],
                    'gps_available': self.total_gps_fixes > 0,
                    'gps_fixes_collected': self.total_gps_fixes,
                    'gps_first_fix_latency_seconds': self.gps_first_fix_latency,
                    'gps_daemon_restart_count': self.restart_counts['gps'],
                    'gps_samples': self._accumulated_data['gps_samples'],
                    'accel_samples': self._accumulated_data['accel_samples'],
                    'gyro_samples': self._accumulated_data['gyro_samples'],
                    'trajectories': trajectory_history,
                    'covariance_snapshots': covariance_history,
                    'final_metrics': {
                        'ekf': self.ekf.get_state(),
                        'complementary': self.complementary.get_state()
                    }
                }

                filename = f"{base_filename}.json.gz"
                temp_filename = f"{filename}.tmp"

                try:
                    with gzip.open(temp_filename, 'wt', encoding='utf-8') as f:
                        json.dump(accumulated_results, f, separators=(',', ':'))

                    # Atomic rename - only clears buffers AFTER confirming save succeeded
                    os.rename(temp_filename, filename)

                    # ✅ Save confirmed - NOW clear samples to free memory
                    if clear_after_save:
                        # Persist chunk to SQLite cache
                        self._persist_sensor_samples('gps', self._accumulated_data['gps_samples'])
                        self._persist_sensor_samples('accel', self._accumulated_data['accel_samples'])
                        if self.enable_gyro:
                            self._persist_sensor_samples('gyro', self._accumulated_data['gyro_samples'])

                        # Reset numpy array indices (reuse pre-allocated memory)
                        gps_count = self.gps_index
                        accel_count = self.accel_index
                        gyro_count = self.gyro_index

                        self.gps_index = 0
                        self.accel_index = 0
                        self.gyro_index = 0

                        # MEMORY OPTIMIZATION: Clear accumulated_data after successful save
                        # Data is preserved on disk in gzip format, no need to keep in memory
                        # This prevents memory growth for long tests (45+ min)
                        gps_count_saved = len(self._accumulated_data['gps_samples'])
                        accel_count_saved = len(self._accumulated_data['accel_samples'])

                        self._accumulated_data['gps_samples'].clear()
                        self._accumulated_data['accel_samples'].clear()
                        self._accumulated_data['gyro_samples'].clear()

                        # FIX 4: REMOVED filter reset - filters should maintain state across auto-saves
                        # Resetting velocity to 0 mid-test creates fake physics

                        print(f"✓ Auto-saved (autosave #{self._accumulated_data['autosave_count']}): {filename} | Saved: {gps_count_saved} GPS + {accel_count_saved} accel | Persisted chunk to SQLite + memory cleared")
                    else:
                        gps_count_display = len(self._accumulated_data['gps_samples'])
                        accel_count_display = len(self._accumulated_data['accel_samples'])
                        print(f"✓ Auto-saved (autosave #{self._accumulated_data['autosave_count']}): {filename} | Total: {gps_count_display} GPS + {accel_count_display} accel")
                except Exception as e:
                    print(f"\n⚠️ WARNING: Auto-save failed (test will continue, data kept in memory): {e}", file=sys.stderr)
                    # Clean up temp file if it exists
                    try:
                        os.remove(temp_filename)
                    except:
                        pass
                    # Do NOT clear buffers on failure - keep data in memory for final save
            else:
                # Final save: Read last auto-save from disk + current buffer contents
                # (accumulated_data is now cleared after each auto-save to save memory)
                if hasattr(self, '_accumulated_data') and self._accumulated_data['autosave_count'] > 0:
                    persisted_gps = self._load_sensor_samples('gps')
                    persisted_accel = self._load_sensor_samples('accel')
                    persisted_gyro = self._load_sensor_samples('gyro') if self.enable_gyro else []

                    final_gps = persisted_gps + self._accumulated_data['gps_samples'] + gps_samples_current
                    final_accel = persisted_accel + self._accumulated_data['accel_samples'] + accel_samples_current
                    final_gyro = persisted_gyro + self._accumulated_data['gyro_samples'] + gyro_samples_current

                    print(f"\n✓ Final save data assembly (SQLite + in-memory):")
                    print(f"  Persisted GPS: {len(persisted_gps)} | Current numpy: {len(gps_samples_current)}")
                    print(f"  Persisted Accel: {len(persisted_accel)} | Current numpy: {len(accel_samples_current)}")
                    if self.enable_gyro:
                        print(f"  Persisted Gyro: {len(persisted_gyro)} | Current numpy: {len(gyro_samples_current)}")

                    results['gps_samples'] = final_gps
                    results['accel_samples'] = final_accel
                    results['gyro_samples'] = final_gyro
                    results['total_autosaves'] = self._accumulated_data['autosave_count']
                else:
                    # No auto-saves occurred, use current buffers (already set in results dict above)
                    pass

            # Final save - both compressed and uncompressed
            try:
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

                # Generate multi-track GPX file
                self._generate_gpx(base_filename)
            except Exception as e:
                print(f"\n✗ ERROR: Final save failed: {e}", file=sys.stderr)
                print(f"⚠️  Test data may be incomplete but test completed", file=sys.stderr)
                # Don't crash - test has completed even if final save failed
            finally:
                if not auto_save:
                    self._close_sensor_cache()
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

    def _generate_gpx(self, base_filename):
        """Generate multi-track GPX file with 4 filter tracks.

        Creates a GPX document with separate tracks for:
        - Raw GPS measurements
        - EKF filtered trajectory
        - ES-EKF filtered trajectory (dead reckoning)
        - Complementary filter trajectory
        """
        try:
            # Build GPX content with multiple tracks
            gpx_lines = [
                '<?xml version="1.0" encoding="UTF-8"?>',
                '<gpx version="1.1" creator="motion_tracker_v2" xmlns="http://www.topografix.com/GPX/1/1">',
                f'  <metadata>',
                f'    <time>{datetime.fromtimestamp(self.start_time).isoformat()}Z</time>',
                f'    <desc>Multi-track trajectory comparison: GPS, EKF, ES-EKF, Complementary</desc>',
                f'  </metadata>'
            ]

            # Track 1: Raw GPS measurements
            gps_points = []
            with self._save_lock:
                if self.gps_index > 0:
                    gps_points = self._numpy_to_list(self.gps_samples, self.gps_index, 'gps')

            if gps_points:
                gpx_lines.append('  <trk>')
                gpx_lines.append('    <name>Raw GPS</name>')
                gpx_lines.append('    <desc>Unfiltered GPS measurements</desc>')
                gpx_lines.append('    <trkseg>')
                for gps in gps_points:
                    gpx_lines.append(f'      <trkpt lat="{gps["latitude"]}" lon="{gps["longitude"]}">')
                    gpx_lines.append(f'        <time>{datetime.fromtimestamp(gps.get("timestamp", self.start_time)).isoformat()}Z</time>')
                    if 'accuracy' in gps:
                        gpx_lines.append(f'        <extensions><accuracy>{gps["accuracy"]:.1f}</accuracy></extensions>')
                    gpx_lines.append('      </trkpt>')
                gpx_lines.append('    </trkseg>')
                gpx_lines.append('  </trk>')

            # Track 2: EKF trajectory
            ekf_traj = self._get_full_trajectory('ekf')
            if ekf_traj:
                gpx_lines.append('  <trk>')
                gpx_lines.append('    <name>EKF 13D</name>')
                gpx_lines.append('    <desc>Extended Kalman Filter trajectory (with gyroscope)</desc>')
                gpx_lines.append('    <trkseg>')
                for point in ekf_traj:
                    gpx_lines.append(f'      <trkpt lat="{point["lat"]}" lon="{point["lon"]}">')
                    if 'timestamp' in point:
                        gpx_lines.append(f'        <time>{datetime.fromtimestamp(point["timestamp"]).isoformat()}Z</time>')
                    if 'uncertainty_m' in point:
                        gpx_lines.append(f'        <extensions><uncertainty>{point["uncertainty_m"]:.1f}</uncertainty></extensions>')
                    gpx_lines.append('      </trkpt>')
                gpx_lines.append('    </trkseg>')
                gpx_lines.append('  </trk>')

            # Track 3: ES-EKF trajectory (dead reckoning during GPS gaps)
            es_traj = self._get_full_trajectory('es_ekf')
            if es_traj:
                gpx_lines.append('  <trk>')
                gpx_lines.append('    <name>ES-EKF 8D</name>')
                gpx_lines.append('    <desc>Error-State EKF trajectory (smooth dead reckoning, primary for GPS gaps)</desc>')
                gpx_lines.append('    <trkseg>')
                for point in es_traj:
                    gpx_lines.append(f'      <trkpt lat="{point["lat"]}" lon="{point["lon"]}">')
                    if 'timestamp' in point:
                        gpx_lines.append(f'        <time>{datetime.fromtimestamp(point["timestamp"]).isoformat()}Z</time>')
                    if 'uncertainty_m' in point:
                        gpx_lines.append(f'        <extensions><uncertainty>{point["uncertainty_m"]:.1f}</uncertainty></extensions>')
                    gpx_lines.append('      </trkpt>')
                gpx_lines.append('    </trkseg>')
                gpx_lines.append('  </trk>')

            # Track 4: Complementary filter trajectory
            comp_traj = self._get_full_trajectory('complementary')
            if comp_traj:
                gpx_lines.append('  <trk>')
                gpx_lines.append('    <name>Complementary</name>')
                gpx_lines.append('    <desc>Complementary filter trajectory (GPS-weighted fusion)</desc>')
                gpx_lines.append('    <trkseg>')
                for point in comp_traj:
                    gpx_lines.append(f'      <trkpt lat="{point["lat"]}" lon="{point["lon"]}">')
                    if 'timestamp' in point:
                        gpx_lines.append(f'        <time>{datetime.fromtimestamp(point["timestamp"]).isoformat()}Z</time>')
                    if 'uncertainty_m' in point:
                        gpx_lines.append(f'        <extensions><uncertainty>{point["uncertainty_m"]:.1f}</uncertainty></extensions>')
                    gpx_lines.append('      </trkpt>')
                gpx_lines.append('    </trkseg>')
                gpx_lines.append('  </trk>')

            gpx_lines.append('</gpx>')

            # Write GPX file
            gpx_filename = f"{base_filename}.gpx"
            with open(gpx_filename, 'w') as f:
                f.write('\n'.join(gpx_lines))

            print(f"✓ Multi-track GPX saved: {gpx_filename}")

        except Exception as e:
            print(f"⚠️  GPX generation failed: {e}", file=sys.stderr)
            # Don't crash - GPX is optional

    def _print_summary(self):
        """Print final comparison summary"""
        print("\n" + "="*100)
        print("FINAL COMPARISON SUMMARY")
        print("="*100)

        ekf_state = self.ekf.get_state()
        comp_state = self.complementary.get_state()

        if self.gps_index > 0:
            first_gps = self.gps_samples[0]
            last_gps = self.gps_samples[self.gps_index - 1]

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
        if self.enable_gyro and len(self.gyro_samples) > 0:
            print(f"\nGyroscope Statistics:")
            print(f"  Total samples:  {len(self.gyro_samples)}")

            # Calculate rotation rate statistics (only magnitude available)
            magnitude_vals = self.gyro_samples['magnitude']

            import statistics
            print(f"  Rotation magnitude (rad/s):  mean={float(np.mean(magnitude_vals)):.4f}, max={float(np.max(magnitude_vals)):.4f}, std={float(np.std(magnitude_vals)):.4f}")
        elif self.enable_gyro:
            print(f"\nGyroscope: Enabled but NO samples collected")

        print("\n" + "="*100)


def main():
    duration = None  # default: continuous (None means run until interrupted)
    enable_gyro = False

    for arg in sys.argv[1:]:
        if arg == '--gyro' or arg == '--enable-gyro':
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
