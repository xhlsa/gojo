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
from queue import Queue, Empty
from datetime import datetime
from collections import deque
from statistics import mean

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
        self.stationary_threshold = 0.1  # m/s²

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
                'last_gps_time': self.last_gps_time
            }


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
    """Background thread for high-frequency accelerometer streaming"""

    def __init__(self, accel_queue, stop_event, sample_rate=50):
        super().__init__(daemon=True)
        self.accel_queue = accel_queue
        self.stop_event = stop_event
        self.sample_rate = sample_rate  # Hz
        self.update_interval = 1.0 / sample_rate

        # Calibration
        self.bias = {'x': 0.0, 'y': 0.0, 'z': 0.0}
        self.gravity = 9.8
        self.calibrated = False

    def calibrate(self, samples=10):
        """Calibrate accelerometer bias"""
        print("Calibrating accelerometer (keep device still)...")
        calibration_samples = []

        for _ in range(samples):
            raw = self.read_raw()
            if raw:
                calibration_samples.append(raw)
            time.sleep(0.2)

        if calibration_samples:
            self.bias['x'] = mean(s['x'] for s in calibration_samples)
            self.bias['y'] = mean(s['y'] for s in calibration_samples)
            self.bias['z'] = mean(s['z'] for s in calibration_samples) - self.gravity
            self.calibrated = True
            print(f"✓ Calibrated. Bias: x={self.bias['x']:.2f}, y={self.bias['y']:.2f}, z={self.bias['z']:.2f}")
        else:
            print("⚠ Calibration failed, using zero bias")

    def read_raw(self):
        """Read raw accelerometer values"""
        try:
            result = subprocess.run(
                ['termux-sensor', '-s', 'accelerometer', '-n', '1'],
                capture_output=True,
                text=True,
                timeout=0.5
            )

            if result.returncode == 0 and result.stdout:
                data = json.loads(result.stdout)

                if 'accelerometer' in data:
                    values = data['accelerometer']['values']
                    return {
                        'x': values[0],
                        'y': values[1],
                        'z': values[2]
                    }
        except Exception:
            pass

        return None

    def read_calibrated(self):
        """Read calibrated accelerometer data"""
        raw = self.read_raw()

        if raw:
            return {
                'x': raw['x'] - self.bias['x'],
                'y': raw['y'] - self.bias['y'],
                'z': raw['z'] - self.bias['z'],
                'magnitude': math.sqrt(
                    (raw['x'] - self.bias['x'])**2 +
                    (raw['y'] - self.bias['y'])**2 +
                    (raw['z'] - self.bias['z'])**2
                ),
                'timestamp': time.time()
            }

        return None

    def run(self):
        """Continuously stream accelerometer data"""
        # Calibrate before starting
        if not self.calibrated:
            self.calibrate()

        while not self.stop_event.is_set():
            accel_data = self.read_calibrated()

            if accel_data:
                self.accel_queue.put(accel_data)

            # Wait before next sample
            time.sleep(self.update_interval)


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

        # Threads
        self.gps_thread = None
        self.accel_thread = None

        # Data storage
        self.samples = []  # GPS-based samples
        self.accel_samples = []  # High-frequency accelerometer samples
        self.battery_samples = []
        self.battery_start = None

        # Tracking state
        self.start_time = None
        self.save_count = 0
        self.last_save_time = None
        self.shutdown_requested = False

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

    def start_threads(self):
        """Start background sensor threads"""
        print("Starting background sensor threads...")

        # Start GPS thread
        self.gps_thread = GPSThread(self.gps_queue, self.stop_event)
        self.gps_thread.start()
        print("✓ GPS thread started")

        # Start accelerometer thread
        self.accel_thread = AccelerometerThread(
            self.accel_queue,
            self.stop_event,
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

                # AUTO-SAVE check
                if current_time - self.last_save_time >= self.auto_save_interval:
                    print(f"\n⏰ Auto-saving data (save #{self.save_count + 1})...")
                    try:
                        self.save_data(auto_save=True)
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
        final_distance = self.samples[-1]['distance']

        print(f"\nSession duration:     {int(duration//60)}m {int(duration%60)}s")
        print(f"Total distance:       {final_distance/1000:.2f} km ({final_distance:.0f} m)")
        print(f"GPS samples:          {len(self.samples)}")
        print(f"Accelerometer samples: {len(self.accel_samples)}")

        if self.samples:
            velocities = [s['velocity'] * 3.6 for s in self.samples]
            print(f"Average speed:        {sum(velocities)/len(velocities):.1f} km/h")
            print(f"Max speed:            {max(velocities):.1f} km/h")

        # Battery stats
        if self.battery_start and self.battery_samples:
            battery_end = self.battery_samples[-1]['battery']
            print(f"\nBattery:")
            print(f"  Start: {self.battery_start['percentage']}%")
            print(f"  End:   {battery_end['percentage']}%")
            print(f"  Drop:  {self.battery_start['percentage'] - battery_end['percentage']}%")

        print(f"\nAuto-saves performed: {self.save_count}")
        print("="*80)

    def save_data(self, auto_save=False):
        """Save tracking data to files"""
        timestamp = self.start_time.strftime('%Y%m%d_%H%M%S')
        base_filename = f"motion_track_v2_{timestamp}"

        # Prepare data
        data = {
            'version': 2,
            'start_time': self.start_time.isoformat(),
            'end_time': datetime.now().isoformat(),
            'total_distance': self.samples[-1]['distance'] if self.samples else 0,
            'gps_samples': self.samples,
            'accel_samples': self.accel_samples,
            'battery_samples': self.battery_samples,
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

        for sample in self.samples:
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

        if len(sys.argv) > 1:
            duration = int(sys.argv[1])
        if len(sys.argv) > 2:
            accel_rate = int(sys.argv[2])

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
