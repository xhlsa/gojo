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
        try:
            json_buffer = ""
            brace_depth = 0

            for line in self.process.stdout:
                # line is already a string (text=True mode)
                json_buffer += line
                brace_depth += line.count('{') - line.count('}')

                if brace_depth == 0 and json_buffer.strip():
                    packets_received += 1
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
                                    self.data_queue.put({
                                        'x': values[0],
                                        'y': values[1],
                                        'z': values[2],
                                        'timestamp': time.time()
                                    }, block=False)
                                    if packets_received <= 3:
                                        print(f"[{self.sensor_type}] Queued packet {packets_received}", file=sys.stderr)
                                    break  # Only process first sensor
                        if packets_received <= 3 and not any(isinstance(v, dict) and 'values' in v for v in data.values()):
                            print(f"[{self.sensor_type}] Packet {packets_received} had no valid sensor data: {list(data.keys())}", file=sys.stderr)
                    except Exception as e:
                        if packets_received <= 3:
                            print(f"[{self.sensor_type}] Parse error on packet {packets_received}: {str(e)[:50]}", file=sys.stderr)
                    json_buffer = ""
        except Exception as e:
            import sys
            print(f"[{self.sensor_type}] _read_loop exception: {e}", file=sys.stderr)

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


def parse_gps():
    """Get GPS data from termux-location"""
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
        self.accel_daemon = PersistentAccelDaemon(delay_ms=50)
        self.gyro_daemon = None  # Will be initialized if enable_gyro=True

        # Data storage - BOUNDED to prevent OOM
        # Max ~15000 accel samples (5 min at 50 Hz), ~300 GPS fixes (5 min at 1/sec)
        self.gps_samples = deque(maxlen=5000)
        self.accel_samples = deque(maxlen=50000)
        self.gyro_samples = deque(maxlen=50000)  # Same size as accel for gyro data
        self.comparison_samples = deque(maxlen=1000)

        # Metrics
        self.last_gps_time = None
        self.start_time = time.time()
        self.last_status_time = time.time()

        # Memory monitoring
        self.process = psutil.Process()
        self.peak_memory = 0

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

        print(f"\n✓ EKF filter initialized")
        print(f"✓ Complementary filter initialized")

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

        print(f"\n✓ Running for {self.duration_minutes} minutes...")

        # Start GPS thread
        gps_thread = threading.Thread(target=self._gps_loop, daemon=True)
        gps_thread.start()

        # Start accel thread
        accel_thread = threading.Thread(target=self._accel_loop, daemon=True)
        accel_thread.start()

        # Start gyro thread (if enabled)
        if self.gyro_daemon:
            gyro_thread = threading.Thread(target=self._gyro_loop, daemon=True)
            gyro_thread.start()

        # Display thread
        display_thread = threading.Thread(target=self._display_loop, daemon=True)
        display_thread.start()

        # Wait for duration
        try:
            time.sleep(self.duration_minutes * 60)
        except KeyboardInterrupt:
            print("\n\nInterrupted by user")
        finally:
            self.stop()

        return True

    def _gps_loop(self):
        """Periodically get GPS fix"""
        last_gps_attempt = 0

        while not self.stop_event.is_set():
            now = time.time()

            # Try GPS every 5 seconds
            if now - last_gps_attempt > 5:
                last_gps_attempt = now
                gps = parse_gps()

                if gps:
                    # Update both filters
                    v1, d1 = self.ekf.update_gps(gps['latitude'], gps['longitude'],
                                                  gps['speed'], gps['accuracy'])
                    v2, d2 = self.complementary.update_gps(gps['latitude'], gps['longitude'],
                                                            gps['speed'], gps['accuracy'])

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

            time.sleep(0.1)

    def _accel_loop(self):
        """Process accelerometer samples"""
        while not self.stop_event.is_set():
            accel_data = self.accel_daemon.get_data(timeout=0.1)

            if accel_data:
                try:
                    # Data now comes pre-extracted as {'x': ..., 'y': ..., 'z': ...}
                    x = float(accel_data.get('x', 0))
                    y = float(accel_data.get('y', 0))
                    z = float(accel_data.get('z', 0))

                    magnitude = (x**2 + y**2 + z**2) ** 0.5

                    # Update both filters
                    v1, d1 = self.ekf.update_accelerometer(magnitude)
                    v2, d2 = self.complementary.update_accelerometer(magnitude)

                    self.accel_samples.append({
                        'timestamp': time.time() - self.start_time,
                        'magnitude': magnitude,
                        'ekf_velocity': v1,
                        'ekf_distance': d1,
                        'comp_velocity': v2,
                        'comp_distance': d2
                    })
                except:
                    pass

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
                    # Log errors but don't crash thread
                    pass

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

        status_msg = (
            f"[{mins:02d}:{secs:02d}] STATUS: Memory={mem_mb:.1f}MB (peak={self.peak_memory:.1f}MB) | "
            f"GPS={gps_count:4d} | Accel={accel_count:5d}"
        )

        if self.enable_gyro:
            status_msg += f" | Gyro={gyro_count:5d}"

        sys.stderr.write(status_msg + "\n")
        sys.stderr.flush()

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
        sensor_info = f"GPS fixes: {len(self.gps_samples)} | Accel samples: {len(self.accel_samples)}"
        if self.enable_gyro:
            sensor_info += f" | Gyro samples: {len(self.gyro_samples)}"
        print(sensor_info)

    def stop(self):
        self.stop_event.set()
        self.accel_daemon.stop()

        # CRITICAL: Verify accelerometer data was collected
        if len(self.accel_samples) == 0:
            print(f"\n✗ FATAL ERROR: Test completed but NO accelerometer samples were collected")
            print(f"  This indicates a sensor hardware or configuration problem")
            print(f"  Verify: termux-sensor -s ACCELEROMETER produces output")
            print(f"  Results will be saved but test is INVALID")
            print()

        self._save_results()

    def _save_results(self):
        """Save results to JSON file"""
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"comparison_{timestamp}.json"

        results = {
            'test_duration': self.duration_minutes,
            'actual_duration': time.time() - self.start_time,
            'peak_memory_mb': self.peak_memory,
            'gps_samples': list(self.gps_samples),  # Convert deque to list
            'accel_samples': list(self.accel_samples),  # Convert deque to list
            'gyro_samples': list(self.gyro_samples) if self.enable_gyro else [],  # Convert deque to list
            'final_metrics': {
                'ekf': self.ekf.get_state(),
                'complementary': self.complementary.get_state()
            }
        }

        with open(filename, 'w') as f:
            json.dump(results, f, indent=2)

        print(f"\n✓ Results saved to: {filename}")
        print(f"✓ Peak memory usage: {self.peak_memory:.1f} MB")

        # Print summary
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
    duration = 5  # default 5 minutes
    enable_gyro = False

    for arg in sys.argv[1:]:
        if arg == '--gyro':
            enable_gyro = True
        elif arg.isdigit():
            duration = int(arg)

    print(f"\nConfiguration:")
    print(f"  Duration: {duration} minutes")
    print(f"  Gyroscope: {'Enabled' if enable_gyro else 'Disabled'}")
    print(f"\nStarting in 2 seconds...")
    time.sleep(2)

    test = FilterComparison(duration_minutes=duration, enable_gyro=enable_gyro)
    test.start()


if __name__ == '__main__':
    main()
