#!/usr/bin/env python3
"""
Test harness for independent 2D Kalman filter system.
Uses existing SensorDaemon infrastructure with gyroscope data.

Run: python test_kalman_2d.py [duration_seconds]
"""

import subprocess
import json
import time
import sys
import threading
import math
from queue import Queue, Empty
from datetime import datetime
from collections import deque

from kalman_2d_independent import TwoStageKalmanFusion


class SensorDaemon:
    """Enhanced sensor daemon that supports both accel and gyro"""

    def __init__(self, sensor_type='accelerometer', delay_ms=20, max_queue_size=1000):
        self.sensor_type = sensor_type
        self.delay_ms = delay_ms
        self.process = None
        self.data_queue = Queue(maxsize=max_queue_size)
        self.reader_thread = None
        self.stop_event = threading.Event()

    def start(self):
        try:
            sensor_map = {
                'accelerometer': 'lsm6dso LSM6DSO Accelerometer Non-wakeup',
                'gyroscope': 'lsm6dso LSM6DSO Gyroscope Non-wakeup'
            }
            sensor_name = sensor_map.get(self.sensor_type, self.sensor_type)

            self.process = subprocess.Popen(
                ['termux-sensor', '-s', sensor_name, '-d', str(self.delay_ms)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1
            )
            self.reader_thread = threading.Thread(target=self._read_stream, daemon=True)
            self.reader_thread.start()
            print(f"✓ Sensor daemon started ({self.sensor_type}, {1000//self.delay_ms:.0f}Hz)")
            return True
        except Exception as e:
            print(f"⚠ Failed to start sensor daemon: {e}")
            return False

    def _read_stream(self):
        if not self.process:
            return

        try:
            json_buffer = ""
            brace_count = 0

            for line in self.process.stdout:
                if self.stop_event.is_set():
                    break

                json_buffer += line
                brace_count += line.count('{') - line.count('}')

                if brace_count == 0 and json_buffer.strip():
                    try:
                        data = json.loads(json_buffer)
                        for sensor_key, sensor_data in data.items():
                            if isinstance(sensor_data, dict) and 'values' in sensor_data:
                                values = sensor_data['values']
                                if len(values) >= 3:
                                    sensor_datum = {
                                        'x': values[0],
                                        'y': values[1],
                                        'z': values[2],
                                        'timestamp': time.time()
                                    }
                                    try:
                                        self.data_queue.put_nowait(sensor_datum)
                                    except:
                                        pass
                        json_buffer = ""
                    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                        json_buffer = ""

        except Exception:
            pass
        finally:
            if self.process:
                try:
                    self.process.terminate()
                except:
                    pass

    def stop(self):
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
        try:
            return self.data_queue.get(timeout=timeout)
        except Empty:
            return None


class KalmanTestRunner:
    """Test the 2D Kalman filter with live sensor data"""

    def __init__(self, duration_seconds=30):
        self.duration = duration_seconds
        self.start_time = None
        self.stop_event = threading.Event()

        # Sensor daemons
        self.accel_daemon = None
        self.gyro_daemon = None
        self.mag_daemon = None

        # Kalman fusion
        self.fusion = TwoStageKalmanFusion(accel_sample_rate=50)

        # Data queues
        self.accel_queue = Queue(maxsize=1000)
        self.gyro_queue = Queue(maxsize=1000)
        self.mag_queue = Queue(maxsize=1000)

        # Threads
        self.accel_thread = None
        self.gyro_thread = None

        # Calibration
        self.calibration_samples = deque(maxlen=50)
        self.calibrated = False

    def start_daemons(self):
        """Start accelerometer and gyroscope sensor daemons"""
        print("\nStarting sensor daemons...")

        self.accel_daemon = SensorDaemon('accelerometer', delay_ms=20)
        if not self.accel_daemon.start():
            print("⚠ Accelerometer daemon failed")
            return False

        self.gyro_daemon = SensorDaemon('gyroscope', delay_ms=20)
        if not self.gyro_daemon.start():
            print("⚠ Gyroscope daemon failed (continuing without gyro)")
            self.gyro_daemon = None

        # Attempt magnetometer (may not be available)
        try:
            self.mag_daemon = SensorDaemon('magnetometer', delay_ms=20)
            self.mag_daemon.start()
        except:
            print("⚠ Magnetometer not available (continuing without mag)")
            self.mag_daemon = None

        return True

    def calibrate_accelerometer(self, duration=3):
        """Calibrate accelerometer - keep device STILL"""
        print(f"\nCalibrating accelerometer ({duration} seconds - keep device STILL)...")

        cal_start = time.time()
        self.calibration_samples.clear()

        while time.time() - cal_start < duration and not self.stop_event.is_set():
            data = self.accel_daemon.get_data(timeout=0.1)
            if data:
                self.calibration_samples.append([data['x'], data['y'], data['z']])
                print(".", end="", flush=True)

        print()

        if len(self.calibration_samples) > 10:
            import numpy as np
            samples = np.array(list(self.calibration_samples))
            self.fusion.calibrate_accelerometer(samples)
            self.calibrated = True
            return True
        else:
            print("⚠ Calibration failed - not enough samples")
            return False

    def reader_thread_func(self, daemon, queue, sensor_name):
        """Background thread to read from sensor daemon"""
        while not self.stop_event.is_set():
            data = daemon.get_data(timeout=0.05)
            if data:
                try:
                    queue.put_nowait(data)
                except:
                    pass

    def run(self):
        """Main test loop"""
        print("\n" + "="*80)
        print("INDEPENDENT 2D KALMAN FILTER TEST")
        print("="*80)

        if not self.start_daemons():
            return

        time.sleep(1)

        if not self.calibrate_accelerometer():
            return

        # Start reader threads
        if self.gyro_daemon:
            self.gyro_thread = threading.Thread(
                target=self.reader_thread_func,
                args=(self.gyro_daemon, self.gyro_queue, "gyro"),
                daemon=True
            )
            self.gyro_thread.start()

        self.start_time = time.time()

        print("\n" + "="*80)
        print("TRACKING...")
        print("="*80)
        print(f"Running for {self.duration} seconds\n")
        print(f"{'Time':<8} | {'Heading':<10} | {'Distance':<10} | {'Speed':<10} | {'Accel':<10}")
        print("-" * 70)

        last_display = self.start_time
        sample_count = 0
        gyro_count = 0

        try:
            while time.time() - self.start_time < self.duration:
                current_time = time.time()

                # Read accelerometer
                accel_data = self.accel_daemon.get_data(timeout=0.01)
                if not accel_data:
                    accel_data = {'x': 0, 'y': 0, 'z': -9.81}

                # Read gyroscope
                gyro_data = self.gyro_queue.get_nowait() if not self.gyro_queue.empty() else {'x': 0, 'y': 0, 'z': 0}

                # Dummy magnetometer (would need real sensor)
                mag_data = {'x': 0, 'y': 1, 'z': 0}  # Pointing north

                # Update Kalman
                if self.calibrated:
                    import numpy as np
                    gyro_rad = np.array([gyro_data['x'], gyro_data['y'], gyro_data['z']])
                    accel_ms2 = np.array([accel_data['x'], accel_data['y'], accel_data['z']])
                    mag_data = np.array([mag_data['x'], mag_data['y'], mag_data['z']])

                    self.fusion.update(gyro_rad, accel_ms2, mag_data, gps_data=None)

                    sample_count += 1

                    # Display update
                    if current_time - last_display >= 1.0:
                        state = self.fusion.get_state()

                        heading_deg = state['orientation']['yaw']
                        distance = state['position']['distance']
                        speed = state['velocity']['magnitude']
                        accel = state['acceleration']['magnitude']

                        elapsed = int(current_time - self.start_time)
                        print(f"{elapsed:<8} | {heading_deg:>8.1f}° | {distance:>8.1f}m | {speed:>8.3f} m/s | {accel:>8.3f}")

                        last_display = current_time

        except KeyboardInterrupt:
            print("\n\nStopped by user")

        finally:
            self.stop_event.set()
            print("\n\nShutting down...")

            if self.accel_daemon:
                self.accel_daemon.stop()
            if self.gyro_daemon:
                self.gyro_daemon.stop()
            if self.mag_daemon:
                self.mag_daemon.stop()

            if self.gyro_thread:
                self.gyro_thread.join(timeout=1)

            # Final state
            if self.calibrated:
                state = self.fusion.get_state()
                print("\n" + "="*80)
                print("FINAL STATE")
                print("="*80)
                print(f"\nOrientation:")
                print(f"  Roll:  {state['orientation']['roll']:>8.1f}°")
                print(f"  Pitch: {state['orientation']['pitch']:>8.1f}°")
                print(f"  Yaw:   {state['orientation']['yaw']:>8.1f}°")

                print(f"\nPosition:")
                print(f"  X: {state['position']['x']:>8.1f} m")
                print(f"  Y: {state['position']['y']:>8.1f} m")
                print(f"  Distance: {state['position']['distance']:>8.1f} m")

                print(f"\nVelocity:")
                print(f"  Vx: {state['velocity']['x']:>8.3f} m/s")
                print(f"  Vy: {state['velocity']['y']:>8.3f} m/s")
                print(f"  Speed: {state['velocity']['magnitude']:>8.3f} m/s")

                print(f"\nAcceleration:")
                print(f"  Ax: {state['acceleration']['x']:>8.3f} m/s²")
                print(f"  Ay: {state['acceleration']['y']:>8.3f} m/s²")
                print(f"  Magnitude: {state['acceleration']['magnitude']:>8.3f} m/s²")

                print(f"\nSamples processed: {sample_count}")
                print("="*80 + "\n")


def main():
    duration = 30
    if len(sys.argv) > 1:
        try:
            duration = int(sys.argv[1])
        except:
            pass

    runner = KalmanTestRunner(duration_seconds=duration)
    runner.run()


if __name__ == "__main__":
    main()
