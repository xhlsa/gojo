#!/usr/bin/env python3
"""
GPS + Accelerometer Sensor Fusion Tracker V2 - Multithreaded Edition
Continuous sensor streaming with background threads for maximum data capture
"""

import subprocess
import json
import gzip
import time
import math
import signal
import os
import threading
import psutil
from queue import Queue, Empty
from datetime import datetime
from collections import deque
from statistics import mean

# Try to import Cython-optimized accelerometer processor
try:
    from accel_processor import FastAccelProcessor
    HAS_CYTHON = True
except ImportError:
    HAS_CYTHON = False

class SensorFusion:
    """
    Fuses GPS and accelerometer data using complementary filtering
    GPS provides ground truth, accelerometer provides high-frequency updates
    """

    def __init__(self, gps_weight=0.7, accel_weight=0.3):
        # Fusion weights (should sum to 1.0)
        self.gps_weight = gps_weight
        self.accel_weight = accel_weight

        # State variables
        self.velocity = 0.0  # m/s
        self.distance = 0.0  # meters
        self.last_time = None

        # GPS state
        self.last_gps_position = None
        self.last_gps_speed = None
        self.last_gps_time = None

        # Accelerometer state
        self.accel_velocity = 0.0
        self.last_accel_time = None

        # Drift correction
        self.velocity_history = deque(maxlen=10)
        self.stationary_threshold = 0.20  # m/s² (filters sensor noise effectively)

        # Stationary tracking (for dynamic recalibration)
        self.is_stationary = False

        # Thread safety
        self.lock = threading.Lock()

    def haversine_distance(self, lat1, lon1, lat2, lon2):
        """Calculate distance between two GPS coordinates in meters"""
        R = 6371000  # Earth radius in meters

        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        delta_phi = math.radians(lat2 - lat1)
        delta_lambda = math.radians(lon2 - lon1)

        a = (math.sin(delta_phi/2) ** 2 +
             math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda/2) ** 2)
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

        return R * c

    def update_gps(self, latitude, longitude, gps_speed=None, gps_accuracy=None):
        """Update with GPS data - thread safe"""
        with self.lock:
            current_time = time.time()

            # Calculate GPS-based velocity if we have previous position
            if self.last_gps_position and self.last_gps_time:
                dt = current_time - self.last_gps_time

                if dt > 0:
                    # Distance from last GPS position
                    dist = self.haversine_distance(
                        self.last_gps_position[0], self.last_gps_position[1],
                        latitude, longitude
                    )

                    # GPS velocity
                    gps_velocity = dist / dt

                    # Use provided GPS speed if available, otherwise calculated
                    if gps_speed is not None:
                        gps_velocity = gps_speed

                    # STATIONARY DETECTION - Filter GPS noise
                    movement_threshold = max(5.0, gps_accuracy * 1.5) if gps_accuracy else 5.0
                    speed_threshold = 0.1  # m/s (~0.36 km/h) - optimized from testing

                    is_stationary = (dist < movement_threshold and gps_velocity < speed_threshold)
                    self.is_stationary = is_stationary  # Track for dynamic recalibration

                    if is_stationary:
                        # Stationary - don't add distance, zero out velocity
                        gps_velocity = 0.0
                        self.velocity = 0.0
                        self.accel_velocity = 0.0
                    else:
                        # Moving - fuse velocities
                        if self.accel_velocity is not None:
                            self.velocity = (self.gps_weight * gps_velocity +
                                           self.accel_weight * self.accel_velocity)
                        else:
                            self.velocity = gps_velocity

                        # Only add distance if we're actually moving
                        self.distance += dist

                        # Reset accelerometer velocity to GPS velocity (drift correction)
                        self.accel_velocity = self.velocity

            # Update GPS state
            self.last_gps_position = (latitude, longitude)
            self.last_gps_time = current_time
            self.last_gps_speed = gps_speed

            return self.velocity, self.distance

    def update_accelerometer(self, accel_magnitude):
        """Update with accelerometer data (forward acceleration in m/s²) - thread safe"""
        with self.lock:
            current_time = time.time()

            if self.last_accel_time is None:
                self.last_accel_time = current_time
                return self.velocity, self.distance

            dt = current_time - self.last_accel_time

            if dt <= 0:
                return self.velocity, self.distance

            # Integrate acceleration to get velocity
            if abs(accel_magnitude) < self.stationary_threshold:
                # Likely stationary, don't integrate
                accel_magnitude = 0

            # Update velocity
            self.accel_velocity += accel_magnitude * dt

            # Prevent negative velocity
            self.accel_velocity = max(0, self.accel_velocity)

            # Update distance (simple integration)
            self.distance += self.accel_velocity * dt

            # If we don't have recent GPS, use accelerometer velocity
            if self.last_gps_time is None or (current_time - self.last_gps_time) > 5.0:
                self.velocity = self.accel_velocity

            self.last_accel_time = current_time

            return self.velocity, self.distance

    def get_state(self):
        """Get current state - thread safe"""
        with self.lock:
            return {
                'velocity': self.velocity,
                'distance': self.distance,
                'accel_velocity': self.accel_velocity,
                'last_gps_time': self.last_gps_time,
                'is_stationary': self.is_stationary
            }


class SensorDaemon:
    """
    Single long-lived sensor daemon process for continuous streaming
    Spawns termux-sensor ONCE and reads continuous JSON output
    Instead of spawning 50 processes per second
    """

    def __init__(self, sensor_type='accelerometer', delay_ms=20, max_queue_size=1000):
        self.sensor_type = sensor_type
        self.delay_ms = delay_ms
        self.process = None
        self.data_queue = Queue(maxsize=max_queue_size)
        self.reader_thread = None
        self.stop_event = threading.Event()

    def start(self):
        """Start the sensor daemon process"""
        try:
            # Start single long-lived termux-sensor process
            self.process = subprocess.Popen(
                ['termux-sensor', '-s', self.sensor_type, '-d', str(self.delay_ms)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1
            )

            # Start reader thread that parses continuous JSON output
            self.reader_thread = threading.Thread(target=self._read_stream, daemon=True)
            self.reader_thread.start()
            print(f"✓ Sensor daemon started ({self.sensor_type}, {1000//self.delay_ms:.0f}Hz)")
            return True
        except Exception as e:
            print(f"⚠ Failed to start sensor daemon: {e}")
            return False

    def _read_stream(self):
        """Read and parse continuous JSON sensor stream (multi-line JSON objects)"""
        if not self.process:
            return

        try:
            json_buffer = ""
            brace_count = 0

            for line in self.process.stdout:
                if self.stop_event.is_set():
                    break

                json_buffer += line

                # Count braces to detect complete JSON objects
                brace_count += line.count('{') - line.count('}')

                # When braces match, we have a complete JSON object
                if brace_count == 0 and json_buffer.strip():
                    try:
                        data = json.loads(json_buffer)

                        # Extract accelerometer values
                        for sensor_key, sensor_data in data.items():
                            if isinstance(sensor_data, dict) and 'values' in sensor_data:
                                values = sensor_data['values']
                                if len(values) >= 3:
                                    accel_data = {
                                        'x': values[0],
                                        'y': values[1],
                                        'z': values[2],
                                        'timestamp': time.time()
                                    }

                                    # Try to put in queue, skip if full (non-blocking)
                                    try:
                                        self.data_queue.put_nowait(accel_data)
                                    except:
                                        # Queue full, skip this sample
                                        pass

                        json_buffer = ""

                    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
                        # Skip malformed JSON but keep trying
                        json_buffer = ""

        except Exception as e:
            pass  # Silently handle stream errors (process termination is normal)
        finally:
            if self.process:
                try:
                    self.process.terminate()
                except:
                    pass

    def stop(self):
        """Stop the sensor daemon"""
        self.stop_event.set()

        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except:
                self.process.kill()

        if self.reader_thread:
            self.reader_thread.join(timeout=2)

    def get_data(self, timeout=None):
        """Get next sensor reading from daemon"""
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
                data = json.loads(result.stdout)
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
        while not self.stop_event.is_set():
            gps_data = self.read_gps()

            if gps_data and gps_data.get('latitude'):
                self.gps_queue.put(gps_data)

            # Wait before next poll (if not stopping)
            self.stop_event.wait(self.update_interval)


class AccelerometerThread(threading.Thread):
    """Background thread for reading from sensor daemon"""

    def __init__(self, accel_queue, stop_event, sensor_daemon, fusion=None, sample_rate=50):
        super().__init__(daemon=True)
        self.accel_queue = accel_queue
        self.stop_event = stop_event
        self.sensor_daemon = sensor_daemon
        self.fusion = fusion  # Reference to fusion for stationary detection
        self.sample_rate = sample_rate  # Hz

        # Calibration
        self.bias = {'x': 0.0, 'y': 0.0, 'z': 0.0}
        self.gravity = 9.8
        self.calibrated = False

        # Dynamic re-calibration (when stationary)
        self.last_recal_time = None
        self.recal_interval = 30  # seconds - check every 30s if stationary
        self.recal_buffer = []
        self.recal_threshold = 0.5  # buffer count before recalibrating

    def calibrate(self, samples=10, silent=False):
        """Calibrate accelerometer bias from daemon readings (magnitude-based)"""
        if not silent:
            print("Calibrating accelerometer (keep device still)...")
        calibration_samples = []

        for _ in range(samples):
            raw = self.sensor_daemon.get_data(timeout=1)
            if raw:
                calibration_samples.append(raw)
            time.sleep(0.2)

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
            if not silent:
                print(f"✓ Calibrated. Bias: x={self.bias['x']:.2f}, y={self.bias['y']:.2f}, z={self.bias['z']:.2f}, Gravity: {self.gravity:.2f} m/s²")
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
        # Calibrate before starting
        if not self.calibrated:
            self.calibrate()

        while not self.stop_event.is_set():
            # Read from daemon (non-blocking)
            raw_data = self.sensor_daemon.get_data(timeout=0.1)

            if raw_data:
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
                if accel_data:
                    try:
                        self.accel_queue.put_nowait(accel_data)
                    except:
                        # Queue full, skip sample
                        pass


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
                data = json.loads(result.stdout)
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

    def __init__(self, auto_save_interval=120, battery_sample_interval=10, accel_sample_rate=50):
        self.auto_save_interval = auto_save_interval  # seconds
        self.battery_sample_interval = battery_sample_interval  # sample battery every N seconds
        self.accel_sample_rate = accel_sample_rate  # Hz

        # Sensor fusion
        self.fusion = SensorFusion()
        self.battery = BatteryReader()

        # Threading
        self.stop_event = threading.Event()
        self.gps_queue = Queue(maxsize=100)
        self.accel_queue = Queue(maxsize=1000)  # Larger for high-frequency data

        # Sensor daemon (single long-lived process)
        self.sensor_daemon = None

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

    def start_threads(self):
        """Start background sensor threads"""
        print("Starting background sensor threads...")

        # Start sensor daemon (single long-lived process for continuous streaming)
        # Calculate delay_ms from desired sample rate
        delay_ms = max(10, int(1000 / self.accel_sample_rate))
        self.sensor_daemon = SensorDaemon(sensor_type='accelerometer', delay_ms=delay_ms)
        if not self.sensor_daemon.start():
            print("⚠ Sensor daemon failed to start, continuing anyway...")

        # Start GPS thread
        self.gps_thread = GPSThread(self.gps_queue, self.stop_event)
        self.gps_thread.start()
        print("✓ GPS thread started")

        # Start accelerometer thread
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
            temp_accel_thread.calibrate()

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
            self.accel_thread = AccelerometerThread(
                self.accel_queue,
                self.stop_event,
                self.sensor_daemon,
                fusion=self.fusion,
                sample_rate=self.accel_sample_rate
            )
            self.accel_thread.start()
            print(f"✓ Accelerometer thread started ({self.accel_sample_rate} Hz)")

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
                            # Use horizontal acceleration as forward acceleration estimate
                            horizontal_accel = math.sqrt(accel_data['x']**2 + accel_data['y']**2)

                            # Update fusion with accelerometer
                            velocity, distance = self.fusion.update_accelerometer(horizontal_accel)

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

        if self.gps_thread:
            self.gps_thread.join(timeout=2)
        if self.accel_thread:
            self.accel_thread.join(timeout=2)

        # Stop sensor daemon
        if self.sensor_daemon:
            self.sensor_daemon.stop()

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
        base_filename = f"motion_track_v2_{timestamp}"

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
                'auto_save_interval': self.auto_save_interval
            }
        }

        if auto_save:
            # Compressed auto-save
            filename = f"{base_filename}.json.gz"
            temp_filename = f"{filename}.tmp"

            with gzip.open(temp_filename, 'wt', encoding='utf-8') as f:
                json.dump(data, f, separators=(',', ':'))

            # Atomic rename
            os.rename(temp_filename, filename)

            # Clear samples after saving to free memory
            if clear_after_save:
                self.samples.clear()
                self.accel_samples.clear()
                # Keep battery samples for final report
        else:
            # Final save - both compressed and uncompressed
            # Uncompressed JSON
            filename_json = f"{base_filename}.json"
            temp_filename = f"{filename_json}.tmp"

            with open(temp_filename, 'w') as f:
                json.dump(data, f, indent=2)

            os.rename(temp_filename, filename_json)

            # Compressed
            filename_gz = f"{base_filename}.json.gz"
            with gzip.open(filename_gz, 'wt', encoding='utf-8') as f:
                json.dump(data, f, separators=(',', ':'))

            # GPX export (GPS samples only)
            self.export_gpx(timestamp)

            print(f"\n✓ Data saved:")
            print(f"  {filename_json}")
            print(f"  {filename_gz}")
            print(f"  motion_track_v2_{timestamp}.gpx")

    def export_gpx(self, timestamp):
        """Export GPS track to GPX format"""
        filename = f"motion_track_v2_{timestamp}.gpx"

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
        accel_rate = 50  # Default 50 Hz

        for arg in sys.argv[1:]:
            if arg == "--test":
                duration = 2  # 2 minutes for testing
            elif arg.isdigit():
                duration = int(arg)
            else:
                try:
                    accel_rate = int(arg)
                except:
                    pass

        print(f"\nConfiguration:")
        if duration:
            print(f"  Duration: {duration} minutes")
        else:
            print(f"  Duration: Continuous (Ctrl+C to stop)")
        print(f"  Accelerometer: {accel_rate} Hz")
        print(f"  Auto-save: Every 2 minutes")
        print("\nStarting in 3 seconds...")
        time.sleep(3)

        tracker = MotionTrackerV2(accel_sample_rate=accel_rate)
        tracker.track(duration_minutes=duration)

    except KeyboardInterrupt:
        print("\n\nExiting...")
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
