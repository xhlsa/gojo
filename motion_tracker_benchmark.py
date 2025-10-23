#!/usr/bin/env python3
"""
Motion Tracker Benchmark - Multi-Rate Comparison
Tests multiple accelerometer sampling rates in parallel to find optimal rate
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
import copy

class SensorFusion:
    """Thread-safe sensor fusion"""

    def __init__(self, gps_weight=0.7, accel_weight=0.3):
        self.gps_weight = gps_weight
        self.accel_weight = accel_weight
        self.velocity = 0.0
        self.distance = 0.0
        self.last_gps_position = None
        self.last_gps_time = None
        self.accel_velocity = 0.0
        self.last_accel_time = None
        self.stationary_threshold = 0.1
        self.lock = threading.Lock()

    def haversine_distance(self, lat1, lon1, lat2, lon2):
        """Calculate distance between two GPS coordinates in meters"""
        R = 6371000
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
        with self.lock:
            current_time = time.time()

            if self.last_gps_position and self.last_gps_time:
                dt = current_time - self.last_gps_time

                if dt > 0:
                    dist = self.haversine_distance(
                        self.last_gps_position[0], self.last_gps_position[1],
                        latitude, longitude
                    )

                    gps_velocity = dist / dt
                    if gps_speed is not None:
                        gps_velocity = gps_speed

                    movement_threshold = max(5.0, gps_accuracy * 1.5) if gps_accuracy else 5.0
                    speed_threshold = 0.1

                    is_stationary = (dist < movement_threshold and gps_velocity < speed_threshold)

                    if is_stationary:
                        gps_velocity = 0.0
                        self.velocity = 0.0
                        self.accel_velocity = 0.0
                    else:
                        if self.accel_velocity is not None:
                            self.velocity = (self.gps_weight * gps_velocity +
                                           self.accel_weight * self.accel_velocity)
                        else:
                            self.velocity = gps_velocity

                        self.distance += dist
                        self.accel_velocity = self.velocity

            self.last_gps_position = (latitude, longitude)
            self.last_gps_time = current_time

            return self.velocity, self.distance

    def update_accelerometer(self, accel_magnitude):
        """Update with accelerometer data"""
        with self.lock:
            current_time = time.time()

            if self.last_accel_time is None:
                self.last_accel_time = current_time
                return self.velocity, self.distance

            dt = current_time - self.last_accel_time

            if dt <= 0:
                return self.velocity, self.distance

            if abs(accel_magnitude) < self.stationary_threshold:
                accel_magnitude = 0

            self.accel_velocity += accel_magnitude * dt
            self.accel_velocity = max(0, self.accel_velocity)
            self.distance += self.accel_velocity * dt

            if self.last_gps_time is None or (current_time - self.last_gps_time) > 5.0:
                self.velocity = self.accel_velocity

            self.last_accel_time = current_time

            return self.velocity, self.distance

    def get_state(self):
        """Get current state"""
        with self.lock:
            return {
                'velocity': self.velocity,
                'distance': self.distance,
                'accel_velocity': self.accel_velocity
            }


class GPSThread(threading.Thread):
    """Shared GPS thread - broadcasts to all rates"""

    def __init__(self, gps_queues, stop_event):
        super().__init__(daemon=True)
        self.gps_queues = gps_queues  # List of queues, one per rate
        self.stop_event = stop_event
        self.update_interval = 1.0

    def read_gps(self, timeout=15):
        """Read GPS data"""
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
                    'speed': data.get('speed'),
                    'bearing': data.get('bearing'),
                    'accuracy': data.get('accuracy'),
                    'timestamp': time.time()
                }
        except Exception:
            pass

        return None

    def run(self):
        """Continuously poll GPS and broadcast to all queues"""
        while not self.stop_event.is_set():
            gps_data = self.read_gps()

            if gps_data and gps_data.get('latitude'):
                # Broadcast to all rate queues
                for queue in self.gps_queues:
                    try:
                        queue.put_nowait(copy.deepcopy(gps_data))
                    except:
                        pass  # Queue full, skip

            self.stop_event.wait(self.update_interval)


class AccelerometerThread(threading.Thread):
    """Accelerometer thread for specific sampling rate"""

    def __init__(self, accel_queue, stop_event, sample_rate, rate_name):
        super().__init__(daemon=True)
        self.accel_queue = accel_queue
        self.stop_event = stop_event
        self.sample_rate = sample_rate
        self.rate_name = rate_name
        self.update_interval = 1.0 / sample_rate
        self.bias = {'x': 0.0, 'y': 0.0, 'z': 0.0}
        self.gravity = 9.8
        self.calibrated = False
        self.sample_count = 0
        self.start_time = None

    def calibrate(self, samples=10):
        """Calibrate accelerometer"""
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

    def read_raw(self):
        """Read raw accelerometer"""
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
        if not self.calibrated:
            self.calibrate()

        self.start_time = time.time()

        while not self.stop_event.is_set():
            accel_data = self.read_calibrated()

            if accel_data:
                self.accel_queue.put(accel_data)
                self.sample_count += 1

            time.sleep(self.update_interval)


class RateTracker:
    """Tracks data for one specific sampling rate"""

    def __init__(self, rate_hz, rate_name):
        self.rate_hz = rate_hz
        self.rate_name = rate_name
        self.fusion = SensorFusion()
        self.gps_samples = []
        self.accel_samples = []
        self.metrics = {
            'sample_count': 0,
            'accel_sample_count': 0,
            'gps_sample_count': 0,
            'start_time': None,
            'end_time': None,
            'max_velocity': 0,
            'max_accel': 0,
            'total_distance': 0,
            'velocity_errors': [],  # Difference from GPS
            'thread_start_time': None,
            'thread_end_time': None
        }

    def process_gps(self, gps_data, elapsed):
        """Process GPS update"""
        velocity, distance = self.fusion.update_gps(
            gps_data['latitude'],
            gps_data['longitude'],
            gps_data.get('speed'),
            gps_data.get('accuracy')
        )

        self.gps_samples.append({
            'timestamp': gps_data['timestamp'],
            'elapsed': elapsed,
            'velocity': velocity,
            'distance': distance,
            'gps': gps_data
        })

        self.metrics['gps_sample_count'] += 1
        self.metrics['max_velocity'] = max(self.metrics['max_velocity'], velocity)
        self.metrics['total_distance'] = distance

    def process_accel(self, accel_data, elapsed):
        """Process accelerometer update"""
        horizontal_accel = math.sqrt(accel_data['x']**2 + accel_data['y']**2)
        velocity, distance = self.fusion.update_accelerometer(horizontal_accel)

        self.accel_samples.append({
            'timestamp': accel_data['timestamp'],
            'elapsed': elapsed,
            'x': accel_data['x'],
            'y': accel_data['y'],
            'z': accel_data['z'],
            'magnitude': accel_data['magnitude'],
            'velocity_estimate': velocity
        })

        self.metrics['accel_sample_count'] += 1
        self.metrics['max_accel'] = max(self.metrics['max_accel'], accel_data['magnitude'])

        # Calculate velocity error vs GPS
        state = self.fusion.get_state()
        if self.gps_samples:
            gps_velocity = self.gps_samples[-1]['velocity']
            error = abs(velocity - gps_velocity)
            self.metrics['velocity_errors'].append(error)


class BenchmarkTracker:
    """Main benchmark tracker running multiple rates"""

    def __init__(self, rates=[10, 25, 50, 100], auto_save_interval=120):
        self.rates = rates
        self.auto_save_interval = auto_save_interval

        # Threading
        self.stop_event = threading.Event()
        self.gps_queues = [Queue(maxsize=100) for _ in rates]
        self.accel_queues = [Queue(maxsize=2000) for _ in rates]

        # Threads
        self.gps_thread = None
        self.accel_threads = []

        # Rate trackers
        self.trackers = {
            f"{rate}hz": RateTracker(rate, f"{rate}hz")
            for rate in rates
        }

        # Session data
        self.start_time = None
        self.save_count = 0
        self.shutdown_requested = False

        # Signal handlers
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        signal_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
        print(f"\n\n⚠ Received {signal_name}, shutting down...")
        self.shutdown_requested = True
        self.stop_event.set()

    def start_threads(self):
        """Start all sensor threads"""
        print(f"\nStarting sensor threads for {len(self.rates)} rates...")

        # Start shared GPS thread
        self.gps_thread = GPSThread(self.gps_queues, self.stop_event)
        self.gps_thread.start()
        print("✓ GPS thread started (shared)")

        # Start accelerometer thread for each rate
        for i, rate in enumerate(self.rates):
            accel_thread = AccelerometerThread(
                self.accel_queues[i],
                self.stop_event,
                rate,
                f"{rate}hz"
            )
            accel_thread.start()
            self.accel_threads.append(accel_thread)
            print(f"✓ Accelerometer thread started @ {rate} Hz")

    def track(self, duration_minutes=None):
        """Main tracking loop"""
        self.start_time = datetime.now()

        print("\n" + "="*80)
        print("MOTION TRACKER BENCHMARK - Multi-Rate Comparison")
        print("="*80)
        print(f"Start time: {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Testing rates: {', '.join(f'{r} Hz' for r in self.rates)}")

        if duration_minutes:
            print(f"Duration: {duration_minutes} minutes")
        else:
            print("Duration: Continuous (Ctrl+C to stop)")

        # Acquire wakelock
        try:
            subprocess.run(['termux-wake-lock'], check=False, capture_output=True)
            print("✓ Wakelock acquired")
        except:
            print("⚠ Could not acquire wakelock")

        # Start threads
        self.start_threads()

        print("\nWaiting for GPS fix...", flush=True)

        # Wait for first GPS fix
        gps_locked = False
        while not gps_locked and not self.shutdown_requested:
            try:
                gps_data = self.gps_queues[0].get(timeout=1)
                if gps_data and gps_data.get('latitude'):
                    print(f"✓ GPS locked: {gps_data['latitude']:.6f}, {gps_data['longitude']:.6f}\n")
                    gps_locked = True
            except Empty:
                pass

        if not gps_locked:
            print("Failed to get GPS lock. Exiting.")
            self.stop_event.set()
            return

        print("Tracking... (Press Ctrl+C to stop)\n")
        print(f"Collecting data for {len(self.rates)} rates simultaneously")
        print(f"Auto-save: every {self.auto_save_interval//60} minutes\n")

        last_save_time = time.time()
        last_display_time = time.time()

        try:
            while not self.shutdown_requested:
                current_time = time.time()
                elapsed = (datetime.now() - self.start_time).total_seconds()

                # Check duration
                if duration_minutes and elapsed > duration_minutes * 60:
                    break

                # Auto-save check
                if current_time - last_save_time >= self.auto_save_interval:
                    print(f"\n⏰ Auto-saving benchmark data...")
                    self.save_data(auto_save=True)
                    last_save_time = current_time
                    self.save_count += 1
                    print(f"✓ Auto-save complete\n")

                # Process all rates
                for i, (rate_name, tracker) in enumerate(self.trackers.items()):
                    # Process GPS queue
                    try:
                        while True:
                            gps_data = self.gps_queues[i].get_nowait()
                            if gps_data and gps_data.get('latitude'):
                                tracker.process_gps(gps_data, elapsed)
                    except Empty:
                        pass

                    # Process accelerometer queue
                    try:
                        batch_count = 0
                        while batch_count < 100:
                            accel_data = self.accel_queues[i].get_nowait()
                            if accel_data:
                                tracker.process_accel(accel_data, elapsed)
                                batch_count += 1
                    except Empty:
                        pass

                # Display update (throttled)
                if current_time - last_display_time >= 5.0:
                    print(f"\n--- Status @ {int(elapsed//60)}:{int(elapsed%60):02d} ---")
                    for rate_name, tracker in self.trackers.items():
                        print(f"{rate_name:>6}: GPS={tracker.metrics['gps_sample_count']:>4}, "
                              f"Accel={tracker.metrics['accel_sample_count']:>6}, "
                              f"Dist={tracker.metrics['total_distance']/1000:.2f}km")
                    last_display_time = current_time

                # Brief sleep
                time.sleep(0.01)

        except KeyboardInterrupt:
            print("\n\nStopped by user.")
        except Exception as e:
            print(f"\n\n⚠ ERROR: {e}")
            import traceback
            traceback.print_exc()

        # Stop threads
        print("\nStopping threads...")
        self.stop_event.set()

        if self.gps_thread:
            self.gps_thread.join(timeout=2)
        for thread in self.accel_threads:
            thread.join(timeout=2)

        print("✓ Threads stopped")

        # Release wakelock
        try:
            subprocess.run(['termux-wake-unlock'], check=False)
            print("✓ Wakelock released")
        except:
            pass

        # Final save
        print("\nGenerating benchmark report...")
        self.print_summary()
        self.save_data(auto_save=False)

    def print_summary(self):
        """Print summary comparison"""
        print("\n" + "="*80)
        print("BENCHMARK SUMMARY")
        print("="*80)

        duration = (datetime.now() - self.start_time).total_seconds()

        print(f"\nDuration: {int(duration//60)}m {int(duration%60)}s")
        print(f"\nPer-Rate Statistics:")
        print(f"{'Rate':<8} | {'GPS':>6} | {'Accel':>8} | {'Distance':>10} | {'Max Spd':>8} | {'Avg Err':>8}")
        print("-" * 80)

        for rate_name, tracker in sorted(self.trackers.items()):
            gps_count = tracker.metrics['gps_sample_count']
            accel_count = tracker.metrics['accel_sample_count']
            distance = tracker.metrics['total_distance'] / 1000
            max_speed = tracker.metrics['max_velocity'] * 3.6

            avg_error = 0
            if tracker.metrics['velocity_errors']:
                avg_error = mean(tracker.metrics['velocity_errors']) * 3.6

            print(f"{rate_name:<8} | {gps_count:>6} | {accel_count:>8} | {distance:>8.2f}km | "
                  f"{max_speed:>6.1f}kph | {avg_error:>6.2f}kph")

        print("="*80)

    def save_data(self, auto_save=False):
        """Save benchmark data"""
        timestamp = self.start_time.strftime('%Y%m%d_%H%M%S')

        # Save each rate separately
        for rate_name, tracker in self.trackers.items():
            data = {
                'rate': rate_name,
                'rate_hz': tracker.rate_hz,
                'start_time': self.start_time.isoformat(),
                'end_time': datetime.now().isoformat(),
                'metrics': tracker.metrics,
                'gps_samples': tracker.gps_samples,
                'accel_samples': tracker.accel_samples
            }

            if auto_save:
                filename = f"benchmark_{rate_name}_{timestamp}.json.gz"
                with gzip.open(filename, 'wt') as f:
                    json.dump(data, f, separators=(',', ':'))
            else:
                filename = f"benchmark_{rate_name}_{timestamp}.json"
                with open(filename, 'w') as f:
                    json.dump(data, f, indent=2)

        # Generate comparison report
        if not auto_save:
            self.generate_comparison_report(timestamp)

    def generate_comparison_report(self, timestamp):
        """Generate comparison report"""
        report = {
            'timestamp': timestamp,
            'start_time': self.start_time.isoformat(),
            'end_time': datetime.now().isoformat(),
            'rates_tested': self.rates,
            'comparison': {}
        }

        for rate_name, tracker in self.trackers.items():
            avg_error = 0
            if tracker.metrics['velocity_errors']:
                avg_error = mean(tracker.metrics['velocity_errors'])

            report['comparison'][rate_name] = {
                'rate_hz': tracker.rate_hz,
                'gps_samples': tracker.metrics['gps_sample_count'],
                'accel_samples': tracker.metrics['accel_sample_count'],
                'total_distance_m': tracker.metrics['total_distance'],
                'max_velocity_ms': tracker.metrics['max_velocity'],
                'max_accel_ms2': tracker.metrics['max_accel'],
                'avg_velocity_error_ms': avg_error,
                'data_file_size_estimate_mb': len(tracker.accel_samples) * 0.0001  # Rough estimate
            }

        filename = f"benchmark_report_{timestamp}.json"
        with open(filename, 'w') as f:
            json.dump(report, f, indent=2)

        print(f"\n✓ Benchmark complete!")
        print(f"  Reports saved:")
        for rate in self.rates:
            print(f"    benchmark_{rate}hz_{timestamp}.json")
        print(f"    benchmark_report_{timestamp}.json")


def main():
    import sys

    print("\n" + "="*80)
    print("MOTION TRACKER BENCHMARK")
    print("Multi-Rate Accelerometer Comparison")
    print("="*80)

    try:
        duration = None
        rates = [10, 25, 50, 100]  # Default rates

        if len(sys.argv) > 1:
            duration = int(sys.argv[1])

        print(f"\nConfiguration:")
        print(f"  Rates: {', '.join(f'{r} Hz' for r in rates)}")
        if duration:
            print(f"  Duration: {duration} minutes")
        else:
            print(f"  Duration: Continuous (Ctrl+C to stop)")
        print("\nStarting in 3 seconds...")
        time.sleep(3)

        tracker = BenchmarkTracker(rates=rates)
        tracker.track(duration_minutes=duration)

    except KeyboardInterrupt:
        print("\n\nExiting...")
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
