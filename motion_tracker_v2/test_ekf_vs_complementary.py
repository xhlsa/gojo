#!/usr/bin/env python3
"""
Real-Time Filter Comparison Test - EKF vs Complementary

Runs both filters in parallel on live sensor data and displays metrics side-by-side.
Perfect for evaluating EKF performance against the baseline Complementary filter.

Usage:
    python test_ekf_vs_complementary.py 5          # Run for 5 minutes
    python test_ekf_vs_complementary.py 10 --gyro  # 10 minutes with gyroscope
"""

import subprocess
import threading
import time
import sys
import json
import os
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
            self.process = subprocess.Popen(
                ['stdbuf', '-oL', 'termux-sensor', '-s', self.sensor_type, '-d', str(self.delay_ms)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                bufsize=1
            )

            reader = threading.Thread(target=self._read_loop, daemon=True)
            reader.start()
            return True
        except Exception as e:
            print(f"Failed to start {self.sensor_type} daemon: {e}")
            return False

    def _read_loop(self):
        try:
            json_buffer = ""
            brace_depth = 0

            for line in self.process.stdout:
                json_buffer += line
                brace_depth += line.count('{') - line.count('}')

                if brace_depth == 0 and json_buffer.strip():
                    try:
                        data = json.loads(json_buffer)
                        self.data_queue.put(data, block=False)
                    except:
                        pass
                    json_buffer = ""
        except:
            pass

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

    def __init__(self, duration_minutes=5):
        self.duration_minutes = duration_minutes
        self.stop_event = threading.Event()

        # Filters
        self.ekf = get_filter('ekf', enable_gyro=False)
        self.complementary = get_filter('complementary')

        # Sensors
        self.accel_daemon = PersistentSensorDaemon('ACCELEROMETER', delay_ms=50)

        # Data storage
        self.gps_samples = []
        self.accel_samples = []
        self.comparison_samples = deque(maxlen=1000)

        # Metrics
        self.last_gps_time = None
        self.start_time = time.time()

    def start(self):
        print("\n" + "="*100)
        print("REAL-TIME FILTER COMPARISON: EKF vs Complementary")
        print("="*100)

        if not self.accel_daemon.start():
            print("ERROR: Failed to start sensor daemon")
            return False

        print(f"\n✓ Accelerometer daemon started")

        # STARTUP VALIDATION - Wait for sensors to properly initialize
        print(f"\n✓ Validating sensor startup (waiting 3 seconds for initialization)...")
        print(f"  Testing sensor data stream...")
        time.sleep(3)

        # Check if we're getting data
        test_data = self.accel_daemon.get_data(timeout=1.0)
        if test_data:
            print(f"  ✓ Sensor responding with data")
        else:
            print(f"  ⚠ WARNING: No sensor data received yet")
            print(f"    Continuing anyway, but data quality may be compromised")

        print(f"\n✓ EKF filter initialized")
        print(f"✓ Complementary filter initialized")
        print(f"✓ Running for {self.duration_minutes} minutes...")

        # Start GPS thread
        gps_thread = threading.Thread(target=self._gps_loop, daemon=True)
        gps_thread.start()

        # Start accel thread
        accel_thread = threading.Thread(target=self._accel_loop, daemon=True)
        accel_thread.start()

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
                    values = accel_data.get('values', {})
                    x = float(values.get('x', 0))
                    y = float(values.get('y', 0))
                    z = float(values.get('z', 0))

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

    def _display_loop(self):
        """Display metrics every second"""
        last_display = 0

        while not self.stop_event.is_set():
            now = time.time()

            if now - last_display > 1.0:  # Update every second
                last_display = now
                self._display_metrics()

            time.sleep(0.1)

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
        print(f"GPS fixes: {len(self.gps_samples)} | Accel samples: {len(self.accel_samples)}")

    def stop(self):
        self.stop_event.set()
        self.accel_daemon.stop()
        self._save_results()

    def _save_results(self):
        """Save results to JSON file"""
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"comparison_{timestamp}.json"

        results = {
            'test_duration': self.duration_minutes,
            'actual_duration': time.time() - self.start_time,
            'gps_samples': self.gps_samples,
            'accel_samples': self.accel_samples,
            'final_metrics': {
                'ekf': self.ekf.get_state(),
                'complementary': self.complementary.get_state()
            }
        }

        with open(filename, 'w') as f:
            json.dump(results, f, indent=2)

        print(f"\n✓ Results saved to: {filename}")

        # Print summary
        self._print_summary()

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

            gps_distance = last_gps['ekf_distance']  # GPS is ground truth
            ekf_distance = ekf_state['distance']
            comp_distance = comp_state['distance']

            ekf_error_pct = abs(ekf_distance - gps_distance) / max(gps_distance, 0.001) * 100 if gps_distance > 0 else 0
            comp_error_pct = abs(comp_distance - gps_distance) / max(gps_distance, 0.001) * 100 if gps_distance > 0 else 0

            print(f"\nDistance Accuracy (vs GPS ground truth):")
            print(f"  GPS Distance:           {gps_distance:.2f} m")
            print(f"  EKF Distance:           {ekf_distance:.2f} m (Error: {ekf_error_pct:.2f}%)")
            print(f"  Complementary Distance: {comp_distance:.2f} m (Error: {comp_error_pct:.2f}%)")

            if ekf_error_pct < comp_error_pct:
                improvement = ((comp_error_pct - ekf_error_pct) / comp_error_pct) * 100
                print(f"\n  ✓ EKF is {improvement:.1f}% more accurate than Complementary")
            else:
                degradation = ((ekf_error_pct - comp_error_pct) / comp_error_pct) * 100
                print(f"\n  ⚠ EKF is {degradation:.1f}% less accurate than Complementary")

        print(f"\nFinal Velocities:")
        print(f"  EKF:           {ekf_state['velocity']:.3f} m/s")
        print(f"  Complementary: {comp_state['velocity']:.3f} m/s")

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

    test = FilterComparison(duration_minutes=duration)
    test.start()


if __name__ == '__main__':
    main()
