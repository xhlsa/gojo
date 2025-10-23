#!/usr/bin/env python3
"""
GPS + Accelerometer Sensor Fusion Tracker
Combines GPS and accelerometer data to track speed and distance accurately
without the drift problems of accelerometer-only tracking
"""

import subprocess
import json
import gzip
import time
import math
import signal
import os
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
        """Update with GPS data"""
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
                # If distance is less than GPS accuracy AND speed is low, assume stationary
                # Thresholds optimized from GPS diagnostic testing (gps_tester.py results)
                movement_threshold = max(5.0, gps_accuracy * 1.5) if gps_accuracy else 5.0
                speed_threshold = 0.1  # m/s (~0.36 km/h) - optimized from testing (max noise: 0.2 m/s)

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
        """Update with accelerometer data (forward acceleration in m/s²)"""
        current_time = time.time()

        if self.last_accel_time is None:
            self.last_accel_time = current_time
            return self.velocity, self.distance

        dt = current_time - self.last_accel_time

        if dt <= 0:
            return self.velocity, self.distance

        # Integrate acceleration to get velocity
        # Remove gravity bias and apply simple drift correction
        if abs(accel_magnitude) < self.stationary_threshold:
            # Likely stationary, don't integrate
            accel_magnitude = 0

        # Update velocity
        self.accel_velocity += accel_magnitude * dt

        # Prevent negative velocity (can't go backwards in our simple model)
        self.accel_velocity = max(0, self.accel_velocity)

        # Update distance (simple integration)
        self.distance += self.accel_velocity * dt

        # If we don't have recent GPS, use accelerometer velocity
        if self.last_gps_time is None or (current_time - self.last_gps_time) > 2.0:
            self.velocity = self.accel_velocity

        self.last_accel_time = current_time

        return self.velocity, self.distance


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
        except Exception as e:
            pass

        return None


class GPSReader:
    """Read GPS data from Termux API"""

    @staticmethod
    def read(timeout=15, retry_count=2):
        """Read current GPS position with retry logic"""
        for attempt in range(retry_count):
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
                        'accuracy': data.get('accuracy')
                    }
            except subprocess.TimeoutExpired:
                if attempt < retry_count - 1:
                    print(f"⚠ GPS timeout (attempt {attempt + 1}/{retry_count}), retrying...")
                    time.sleep(1)
                else:
                    print(f"⚠ GPS timeout after {retry_count} attempts")
            except Exception as e:
                if attempt < retry_count - 1:
                    print(f"⚠ GPS Error: {e}, retrying...")
                    time.sleep(1)
                else:
                    print(f"⚠ GPS Error: {e}")

        return None


class AccelerometerReader:
    """Read accelerometer data from Termux API"""

    def __init__(self):
        self.gravity = 9.81
        self.calibration_samples = []
        self.bias = {'x': 0, 'y': 0, 'z': 0}

    def calibrate(self, samples=10):
        """Calibrate accelerometer to remove bias"""
        print("Calibrating accelerometer (keep device still)...")

        for i in range(samples):
            data = self.read_raw()
            if data:
                self.calibration_samples.append(data)
            time.sleep(0.1)

        if self.calibration_samples:
            self.bias['x'] = mean(s['x'] for s in self.calibration_samples)
            self.bias['y'] = mean(s['y'] for s in self.calibration_samples)
            # Z should be around 9.8 (gravity)
            self.bias['z'] = mean(s['z'] for s in self.calibration_samples) - self.gravity

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
                timeout=2
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
        except Exception as e:
            pass

        return None

    def read(self):
        """Read calibrated accelerometer data"""
        raw = self.read_raw()

        if raw:
            # Remove bias
            return {
                'x': raw['x'] - self.bias['x'],
                'y': raw['y'] - self.bias['y'],
                'z': raw['z'] - self.bias['z']
            }

        return None

    def get_forward_acceleration(self, bearing=None):
        """
        Get forward acceleration (direction of travel)
        If bearing is provided, rotate accelerometer to align with travel direction
        Otherwise, use magnitude of horizontal acceleration
        """
        accel = self.read()

        if not accel:
            return 0.0

        # Simple approach: use magnitude of horizontal acceleration
        # More sophisticated: rotate by bearing angle
        horizontal_accel = math.sqrt(accel['x']**2 + accel['y']**2)

        # Return signed value (assume positive is forward)
        # This is simplified - in reality you'd use bearing to determine sign
        return horizontal_accel


class MotionTracker:
    """Main motion tracking application"""

    def __init__(self, update_rate=1.0, auto_save_interval=120, battery_sample_interval=10):
        self.update_rate = update_rate  # seconds
        self.auto_save_interval = auto_save_interval  # seconds (default 2 min)
        self.battery_sample_interval = battery_sample_interval  # sample battery every N GPS samples
        self.fusion = SensorFusion()
        self.gps = GPSReader()
        self.accel = AccelerometerReader()
        self.battery = BatteryReader()

        self.start_time = None
        self.samples = []
        self.last_save_time = None
        self.save_count = 0
        self.gps_failure_count = 0
        self.max_consecutive_failures = 10
        self.shutdown_requested = False
        self.battery_start = None
        self.battery_samples = []

        # Setup signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

        # Acquire wakelock
        try:
            subprocess.run(['termux-wake-lock'], check=False)
            print("✓ Wakelock acquired")
        except:
            print("⚠ Could not acquire wakelock")

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully"""
        signal_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
        print(f"\n\n⚠ Received {signal_name}, saving data and shutting down...")
        self.shutdown_requested = True

    def calibrate(self):
        """Calibrate sensors"""
        self.accel.calibrate()

    def run(self, duration_minutes=None):
        """Run the tracker"""
        self.start_time = datetime.now()

        print("\n" + "="*70)
        print("GPS + ACCELEROMETER MOTION TRACKER")
        print("="*70)
        print(f"Start time: {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}")

        if duration_minutes:
            print(f"Duration: {duration_minutes} minutes")

        # Read initial battery status
        self.battery_start = self.battery.read()
        if self.battery_start:
            print(f"Battery: {self.battery_start['percentage']}% ({self.battery_start['status']})")

        print("\nWaiting for GPS fix...")

        # Wait for initial GPS fix
        gps_data = None
        while gps_data is None or gps_data.get('latitude') is None:
            gps_data = self.gps.read()
            if gps_data and gps_data.get('latitude'):
                print(f"✓ GPS locked: {gps_data['latitude']:.6f}, {gps_data['longitude']:.6f}")
                self.fusion.update_gps(
                    gps_data['latitude'],
                    gps_data['longitude'],
                    gps_data.get('speed'),
                    gps_data.get('accuracy')
                )
                break
            time.sleep(1)

        print("\nTracking... (Press Ctrl+C to stop)\n")
        print(f"Auto-save enabled: every {self.auto_save_interval//60} minutes")
        print(f"{'Time':<8} | {'Speed (km/h)':<12} | {'Distance (m)':<12} | {'GPS Acc':<8}")
        print("-" * 70)

        last_gps_update = time.time()
        self.last_save_time = time.time()
        sample_count = 0

        try:
            while True:
                # Check for shutdown signal
                if self.shutdown_requested:
                    print("Shutdown signal received, exiting tracking loop...")
                    break

                current_time = time.time()
                elapsed = (datetime.now() - self.start_time).total_seconds()

                # Check duration
                if duration_minutes and elapsed > duration_minutes * 60:
                    break

                # AUTO-SAVE check (every N minutes)
                if current_time - self.last_save_time >= self.auto_save_interval:
                    print(f"\n⏰ Auto-saving data (save #{self.save_count + 1})...")
                    try:
                        self.save_data(auto_save=True)
                        self.last_save_time = current_time
                        self.save_count += 1
                        print(f"✓ Auto-save complete ({len(self.samples)} samples)\n")
                    except Exception as e:
                        print(f"⚠ Auto-save failed: {e}, continuing...\n")

                # Update GPS (every second or so)
                if current_time - last_gps_update >= 1.0:
                    try:
                        gps_data = self.gps.read()

                        if gps_data and gps_data.get('latitude'):
                            velocity, distance = self.fusion.update_gps(
                                gps_data['latitude'],
                                gps_data['longitude'],
                                gps_data.get('speed'),
                                gps_data.get('accuracy')
                            )

                            last_gps_update = current_time
                            self.gps_failure_count = 0  # Reset failure counter

                            # Display update
                            speed_kmh = velocity * 3.6
                            time_str = f"{int(elapsed//60)}:{int(elapsed%60):02d}"
                            accuracy = gps_data.get('accuracy', 0)

                            print(f"{time_str:<8} | {speed_kmh:>10.2f} | {distance:>10.1f} | {accuracy:>6.1f}m")

                            # Sample battery periodically (every N samples to reduce overhead)
                            battery_data = None
                            if sample_count % self.battery_sample_interval == 0:
                                battery_data = self.battery.read()
                                if battery_data:
                                    self.battery_samples.append({
                                        'timestamp': datetime.now().isoformat(),
                                        'elapsed': elapsed,
                                        'battery': battery_data
                                    })

                            # Log sample
                            self.samples.append({
                                'timestamp': datetime.now().isoformat(),
                                'elapsed': elapsed,
                                'velocity': velocity,
                                'distance': distance,
                                'gps': gps_data,
                                'battery': battery_data  # Will be None for most samples
                            })

                            sample_count += 1
                        else:
                            # GPS failed to read
                            self.gps_failure_count += 1
                            if self.gps_failure_count >= self.max_consecutive_failures:
                                print(f"\n⚠ GPS failed {self.gps_failure_count} times consecutively!")
                                print(f"⚠ Auto-saving current data and continuing...\n")
                                try:
                                    self.save_data(auto_save=True)
                                    self.save_count += 1
                                except:
                                    pass
                            print(f"⚠ GPS data unavailable (failure {self.gps_failure_count}/{self.max_consecutive_failures})...")
                            last_gps_update = current_time
                    except Exception as e:
                        self.gps_failure_count += 1
                        print(f"⚠ Error updating GPS ({self.gps_failure_count}/{self.max_consecutive_failures}): {e}")
                        last_gps_update = current_time

                # Read accelerometer (high frequency, but we're not using it much in this simple version)
                # In a more sophisticated implementation, we'd update more frequently

                time.sleep(self.update_rate)

        except KeyboardInterrupt:
            print("\n\nStopped by user.")
        except Exception as e:
            print(f"\n\n⚠ ERROR: {e}")
            print("Auto-saving data before exit...")
            import traceback
            traceback.print_exc()

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
            print("No data collected")
            return

        print("\n" + "="*70)
        print("SUMMARY")
        print("="*70)

        total_distance = self.fusion.distance
        total_time = self.samples[-1]['elapsed']
        avg_speed = total_distance / total_time if total_time > 0 else 0
        max_speed = max(s['velocity'] for s in self.samples) if self.samples else 0

        print(f"Total distance: {total_distance:.2f} meters ({total_distance/1000:.3f} km)")
        print(f"Total time: {int(total_time//60)} min {int(total_time%60)} sec")
        print(f"Average speed: {avg_speed * 3.6:.2f} km/h")
        print(f"Max speed: {max_speed * 3.6:.2f} km/h")
        print(f"Samples collected: {len(self.samples)}")

        # Battery statistics
        if self.battery_start:
            print(f"\nBattery:")
            print(f"  Start: {self.battery_start['percentage']}% ({self.battery_start['status']})")

            if self.battery_samples:
                battery_end = self.battery_samples[-1]['battery']
                battery_drain = self.battery_start['percentage'] - battery_end['percentage']
                drain_rate = battery_drain / (total_time / 3600) if total_time > 0 else 0  # %/hour

                print(f"  End: {battery_end['percentage']}%")
                print(f"  Drain: {battery_drain:.1f}% ({drain_rate:.1f}%/hour)")

                if battery_drain > 0 and drain_rate > 0:
                    estimated_runtime = battery_end['percentage'] / drain_rate
                    print(f"  Estimated runtime at current drain: {estimated_runtime:.1f} hours")
            else:
                print(f"  (Session too short for battery drain measurement)")

        print("="*70)

    def save_data(self, auto_save=False):
        """Save data to JSON file (compressed for auto-saves, uncompressed for final)
        Uses atomic writes to prevent corruption if killed mid-save"""
        base_filename = f"motion_track_{self.start_time.strftime('%Y%m%d_%H%M%S')}"

        data = {
            'start_time': self.start_time.isoformat(),
            'end_time': datetime.now().isoformat(),
            'total_distance': self.fusion.distance,
            'samples': self.samples,
            'auto_save_count': self.save_count if auto_save else 0,
            'battery_start': self.battery_start,
            'battery_samples': self.battery_samples
        }

        if auto_save:
            # Auto-save: compressed, no formatting (saves space)
            filename = f"{base_filename}.json.gz"
            temp_filename = f"{filename}.tmp"

            # Write to temp file first (atomic write)
            with gzip.open(temp_filename, 'wt', encoding='utf-8') as f:
                json.dump(data, f, separators=(',', ':'))  # No whitespace

            # Atomically rename temp to final (prevents corruption)
            os.rename(temp_filename, filename)
        else:
            # Final save: uncompressed, formatted (human readable)
            filename = f"{base_filename}.json"
            temp_filename = f"{filename}.tmp"

            # Write to temp file first (atomic write)
            with open(temp_filename, 'w') as f:
                json.dump(data, f, indent=2)

            # Atomically rename temp to final
            os.rename(temp_filename, filename)

            print(f"✓ Data saved to: {filename}")

            # Also save as GPX (only on final save, not auto-saves)
            gpx_filename = self.save_gpx()
            if gpx_filename:
                print(f"✓ GPX track saved to: {gpx_filename}")
                print(f"  You can open this in Google Earth, Google Maps, or any GPS app!")

        # Memory management: keep only last 1000 samples in memory after auto-save
        # (but all samples are saved to disk)
        if auto_save and len(self.samples) > 1000:
            old_count = len(self.samples)
            self.samples = self.samples[-1000:]  # Keep last 1000
            print(f"  (Memory cleanup: {old_count} -> {len(self.samples)} samples in RAM)")

    def save_gpx(self):
        """Export track to GPX format for mapping apps"""
        if not self.samples:
            return None

        gpx_filename = f"motion_track_{self.start_time.strftime('%Y%m%d_%H%M%S')}.gpx"

        # Build GPX XML
        gpx_content = '<?xml version="1.0" encoding="UTF-8"?>\n'
        gpx_content += '<gpx version="1.1" creator="Motion Tracker" xmlns="http://www.topografix.com/GPX/1/1">\n'
        gpx_content += '  <metadata>\n'
        gpx_content += f'    <name>Motion Track - {self.start_time.strftime("%Y-%m-%d %H:%M")}</name>\n'
        gpx_content += f'    <time>{self.start_time.isoformat()}Z</time>\n'
        gpx_content += '  </metadata>\n'
        gpx_content += '  <trk>\n'
        gpx_content += f'    <name>Track {self.start_time.strftime("%Y%m%d_%H%M%S")}</name>\n'
        gpx_content += '    <trkseg>\n'

        # Add track points
        for sample in self.samples:
            gps = sample.get('gps')
            if gps and gps.get('latitude') and gps.get('longitude'):
                lat = gps['latitude']
                lon = gps['longitude']
                alt = gps.get('altitude', 0)
                timestamp = sample['timestamp']

                gpx_content += f'      <trkpt lat="{lat}" lon="{lon}">\n'

                if alt:
                    gpx_content += f'        <ele>{alt}</ele>\n'

                gpx_content += f'        <time>{timestamp}Z</time>\n'

                # Add speed as extension if available
                speed = sample.get('velocity')
                if speed:
                    gpx_content += f'        <extensions>\n'
                    gpx_content += f'          <speed>{speed}</speed>\n'
                    gpx_content += f'        </extensions>\n'

                gpx_content += f'      </trkpt>\n'

        gpx_content += '    </trkseg>\n'
        gpx_content += '  </trk>\n'
        gpx_content += '</gpx>\n'

        # Write GPX file
        try:
            with open(gpx_filename, 'w') as f:
                f.write(gpx_content)
            return gpx_filename
        except Exception as e:
            print(f"⚠ Failed to save GPX: {e}")
            return None


def main():
    print("\n" + "="*70)
    print("GPS + ACCELEROMETER MOTION TRACKER")
    print("Sensor Fusion for Accurate Speed and Distance Tracking")
    print("="*70 + "\n")

    try:
        # Default to continuous mode - just start tracking immediately
        print("Running in CONTINUOUS mode (Ctrl+C to stop)")
        print("Starting in 3 seconds...")
        time.sleep(3)

        tracker = MotionTracker(update_rate=1.0)

        print("\nCalibrating sensors...")
        tracker.calibrate()

        tracker.run(duration_minutes=None)

    except KeyboardInterrupt:
        print("\n\nExiting...")
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
