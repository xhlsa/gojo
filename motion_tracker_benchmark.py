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
import psutil
from queue import Queue, Empty
from datetime import datetime
from collections import deque
from statistics import mean
from concurrent.futures import ThreadPoolExecutor

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
                        # Avoid deepcopy overhead - put same object (read-only)
                        queue.put_nowait(gps_data)
                    except:
                        pass  # Queue full, skip

            self.stop_event.wait(self.update_interval)


class AccelerometerThread(threading.Thread):
    """Accelerometer thread for specific sampling rate"""

    # Class-level subprocess pool to prevent concurrent process spawning
    # Increased to 12 workers to handle high subprocess spawn rate (~185/sec)
    # 4 accel threads + queue backlog management
    _subprocess_pool = ThreadPoolExecutor(max_workers=12, thread_name_prefix="accel_subprocess")
    _init_lock = threading.Lock()
    _pool_queue_limit = 30  # Max pending jobs before backpressure kicks in

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
        self.dropped_count = 0
        self.start_time = None

    @staticmethod
    def _read_raw_subprocess():
        """Static method to run subprocess (can be safely submitted to pool)"""
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

    def calibrate(self, samples=10):
        """Calibrate accelerometer (using subprocess pool to limit concurrency)"""
        calibration_samples = []

        # Use init lock to serialize calibration across threads
        with AccelerometerThread._init_lock:
            for _ in range(samples):
                # Submit read to thread pool to serialize subprocess calls
                future = AccelerometerThread._subprocess_pool.submit(
                    AccelerometerThread._read_raw_subprocess
                )
                try:
                    raw = future.result(timeout=1.0)
                    if raw:
                        calibration_samples.append(raw)
                except Exception:
                    pass  # Subprocess failed, continue
                time.sleep(0.1)  # Small delay between reads

        if calibration_samples:
            self.bias['x'] = mean(s['x'] for s in calibration_samples)
            self.bias['y'] = mean(s['y'] for s in calibration_samples)
            self.bias['z'] = mean(s['z'] for s in calibration_samples) - self.gravity
            self.calibrated = True

    def read_raw(self):
        """Read raw accelerometer via thread pool with backpressure"""
        try:
            # Check pool saturation - skip read if too many pending jobs
            # This prevents cascade failure from queue buildup
            if self._subprocess_pool._work_queue.qsize() > self._pool_queue_limit:
                return None  # Backpressure: skip this read

            # Submit to pool instead of running directly
            future = AccelerometerThread._subprocess_pool.submit(
                AccelerometerThread._read_raw_subprocess
            )
            # Non-blocking with short timeout to prevent queuing delays
            return future.result(timeout=0.6)
        except Exception:
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
                try:
                    # Non-blocking put with timeout to prevent thread stalls
                    self.accel_queue.put(accel_data, timeout=0.1)
                    self.sample_count += 1
                except:
                    # Queue full, drop sample and track it
                    self.dropped_count += 1

            time.sleep(self.update_interval)


class RateTracker:
    """Tracks data for one specific sampling rate"""

    def __init__(self, rate_hz, rate_name, max_stored_samples=1000):
        self.rate_hz = rate_hz
        self.rate_name = rate_name
        self.fusion = SensorFusion()
        # Use deques with max size to prevent unlimited memory growth
        self.max_stored_samples = max_stored_samples
        self.gps_samples = deque(maxlen=max_stored_samples)
        self.accel_samples = deque(maxlen=max_stored_samples)
        self.metrics = {
            'sample_count': 0,
            'accel_sample_count': 0,
            'gps_sample_count': 0,
            'start_time': None,
            'end_time': None,
            'max_velocity': 0,
            'max_accel': 0,
            'total_distance': 0,
            'velocity_errors': deque(maxlen=max_stored_samples),  # Bounded error tracking
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
            self.metrics['velocity_errors'].append(error)  # Deque handles size limit auto


class BenchmarkTracker:
    """Main benchmark tracker running multiple rates"""

    def __init__(self, rates=[10, 25, 50, 100], auto_save_interval=120):
        self.rates = rates
        self.auto_save_interval = auto_save_interval

        # Threading
        self.stop_event = threading.Event()
        # Queue sizes tuned to maintain 50-second buffers per rate (prevents sample dropping)
        self.gps_queues = [Queue(maxsize=50) for _ in rates]
        # Accel queue sizes: sufficient for 50sec buffer at each rate
        self.accel_queues = [Queue(maxsize=self._calculate_accel_queue_size(rate)) for rate in rates]

        self.dropped_samples = {f"{rate}hz": 0 for rate in rates}
        self.queue_overflows = {f"{rate}hz": 0 for rate in rates}

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
        self.memory_threshold = 80  # Percent - trigger warning/throttle at 80%

        # Signal handlers
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    def _calculate_accel_queue_size(self, rate_hz):
        """Calculate queue size to maintain 10-second buffer"""
        # 10 seconds worth of samples at this rate (reduced from 50 to prevent memory buildup)
        return int(rate_hz * 10)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        signal_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
        print(f"\n\n⚠ Received {signal_name}, shutting down...")
        self.shutdown_requested = True
        self.stop_event.set()

    def check_memory(self):
        """Check memory usage and return throttle level (0=normal, 1=warning, 2=critical)"""
        try:
            memory = psutil.virtual_memory()
            if memory.percent > 85:
                return 2  # Critical - reduce batch sizes
            elif memory.percent > self.memory_threshold:
                return 1  # Warning - slight reduction
            return 0  # OK - process normally
        except:
            return 0  # Can't check, assume OK

    def start_threads(self):
        """Start all sensor threads sequentially to prevent memory spikes"""
        print(f"\nStarting sensor threads for {len(self.rates)} rates (sequential init)...")

        # Start shared GPS thread
        self.gps_thread = GPSThread(self.gps_queues, self.stop_event)
        self.gps_thread.start()
        print("✓ GPS thread started (shared)")

        # Start accelerometer threads ONE AT A TIME to prevent subprocess exhaustion
        for i, rate in enumerate(self.rates):
            print(f"\n  Initializing {rate}Hz accelerometer thread ({i+1}/{len(self.rates)})...", flush=True)

            accel_thread = AccelerometerThread(
                self.accel_queues[i],
                self.stop_event,
                rate,
                f"{rate}hz"
            )
            accel_thread.start()
            self.accel_threads.append(accel_thread)

            # Wait for calibration to complete before starting next thread
            # This prevents 4 threads all trying to calibrate simultaneously
            time.sleep(2.0)
            print(f"✓ Accelerometer thread started @ {rate} Hz")

        print(f"\n✓ All {len(self.rates)} accelerometer threads initialized")

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

        print("\nWaiting for GPS fix (or timeout in 10s)...", flush=True)

        # Wait for first GPS fix (with timeout for testing)
        gps_locked = False
        gps_wait_start = time.time()
        while not gps_locked and not self.shutdown_requested:
            try:
                gps_data = self.gps_queues[0].get(timeout=1)
                if gps_data and gps_data.get('latitude'):
                    print(f"✓ GPS locked: {gps_data['latitude']:.6f}, {gps_data['longitude']:.6f}\n")
                    gps_locked = True
            except Empty:
                if time.time() - gps_wait_start > 10:
                    print("⚠ GPS timeout - continuing without GPS lock (test mode)\n")
                    break

        if not gps_locked:
            print("⚠ No GPS lock available, running in simulation mode")

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

                # Check memory before processing
                memory_throttle = self.check_memory()

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

                    # Process accelerometer queue with dynamic batching based on memory
                    # Always process (graceful degradation) - never completely stop
                    try:
                        batch_count = 0
                        queue_size = self.accel_queues[i].qsize()

                        # Determine batch size based on memory pressure
                        if memory_throttle == 2:  # Critical
                            max_batch = min(5, queue_size + 1)  # Very conservative
                        elif memory_throttle == 1:  # Warning
                            max_batch = min(10, queue_size + 1)  # Reduced
                        else:  # Normal
                            max_batch = min(50, queue_size + 1)  # Aggressive processing

                        while batch_count < max_batch:
                            accel_data = self.accel_queues[i].get_nowait()
                            if accel_data:
                                tracker.process_accel(accel_data, elapsed)
                                batch_count += 1
                    except Empty:
                        pass

                    # Detect queue overflow (dropped samples)
                    if queue_size > self.accel_queues[i].maxsize * 0.9:
                        self.queue_overflows[rate_name] += 1

                # Display update (throttled)
                if current_time - last_display_time >= 5.0:
                    print(f"\n--- Status @ {int(elapsed//60)}:{int(elapsed%60):02d} ---")
                    for rate_name, tracker in self.trackers.items():
                        warning = ""
                        if self.queue_overflows[rate_name] > 0:
                            warning = f" ⚠ OVERFLOW"
                        queue_fill = (self.accel_queues[self.rates.index(tracker.rate_hz)].qsize() /
                                     self.accel_queues[self.rates.index(tracker.rate_hz)].maxsize) * 100
                        print(f"{rate_name:>6}: GPS={tracker.metrics['gps_sample_count']:>4}, "
                              f"Accel={tracker.metrics['accel_sample_count']:>6}, "
                              f"Dist={tracker.metrics['total_distance']/1000:.2f}km, "
                              f"Queue={queue_fill:.0f}%{warning}")
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

        # Shutdown subprocess pool properly
        try:
            AccelerometerThread._subprocess_pool.shutdown(wait=True, cancel_futures=True)
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
        print(f"{'Rate':<8} | {'GPS':>6} | {'Accel':>8} | {'Distance':>10} | {'Max Spd':>8} | {'Drops':>8} | {'Overflows':>10}")
        print("-" * 90)

        for rate_name, tracker in sorted(self.trackers.items()):
            gps_count = tracker.metrics['gps_sample_count']
            accel_count = tracker.metrics['accel_sample_count']
            distance = tracker.metrics['total_distance'] / 1000
            max_speed = tracker.metrics['max_velocity'] * 3.6
            overflows = self.queue_overflows.get(rate_name, 0)

            # Find corresponding accel thread to get dropped count
            dropped = 0
            for thread in self.accel_threads:
                if thread.rate_name == rate_name:
                    dropped = thread.dropped_count
                    break

            overflow_warning = ""
            if overflows > 0:
                overflow_warning = f"{overflows:>10} ⚠"
            else:
                overflow_warning = f"{overflows:>10}"

            print(f"{rate_name:<8} | {gps_count:>6} | {accel_count:>8} | {distance:>8.2f}km | "
                  f"{max_speed:>6.1f}kph | {dropped:>8} | {overflow_warning}")

        print("="*80)

        # Report on queue overflows
        if any(self.queue_overflows.values()):
            print("\n⚠ Queue Overflow Detected:")
            for rate_name, overflows in self.queue_overflows.items():
                if overflows > 0:
                    rate_hz = self.trackers[rate_name].rate_hz
                    current_qsize = self._calculate_accel_queue_size(rate_hz)
                    print(f"  {rate_name}: {overflows} overflow events - consider increasing queue from {current_qsize}")
            print("\nThis indicates samples were dropped. Increase queue sizes or reduce number of parallel rates.")
        else:
            print("\n✓ No queue overflows - all samples captured successfully!")

    def save_data(self, auto_save=False):
        """Save benchmark data with atomic writes"""
        # Use current timestamp for auto-saves to avoid overwrites
        # Use start timestamp for final saves to group data
        if auto_save:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        else:
            timestamp = self.start_time.strftime('%Y%m%d_%H%M%S')

        # Save each rate separately
        for rate_name, tracker in self.trackers.items():
            # Convert deques to lists for JSON serialization
            metrics = dict(tracker.metrics)
            metrics['velocity_errors'] = list(metrics['velocity_errors'])

            data = {
                'rate': rate_name,
                'rate_hz': tracker.rate_hz,
                'start_time': self.start_time.isoformat(),
                'end_time': datetime.now().isoformat(),
                'metrics': metrics,
                'gps_samples': list(tracker.gps_samples),
                'accel_samples': list(tracker.accel_samples)
            }

            if auto_save:
                # Use atomic write with temp file for auto-saves
                filename = f"benchmark_{rate_name}_{timestamp}.json.gz"
                temp_filename = filename + ".tmp"
                try:
                    with gzip.open(temp_filename, 'wt') as f:
                        json.dump(data, f, separators=(',', ':'))
                    os.replace(temp_filename, filename)  # Atomic rename
                except Exception as e:
                    print(f"⚠ Auto-save error for {rate_name}: {e}")
                    if os.path.exists(temp_filename):
                        os.remove(temp_filename)
            else:
                filename = f"benchmark_{rate_name}_{timestamp}.json"
                temp_filename = filename + ".tmp"
                try:
                    with open(temp_filename, 'w') as f:
                        json.dump(data, f, indent=2)
                    os.replace(temp_filename, filename)  # Atomic rename
                except Exception as e:
                    print(f"⚠ Save error for {rate_name}: {e}")
                    if os.path.exists(temp_filename):
                        os.remove(temp_filename)

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
        test_mode = False

        # Parse arguments: python script.py [duration_minutes] [--test]
        for arg in sys.argv[1:]:
            if arg == "--test":
                test_mode = True
                rates = [10]  # Single slow rate for testing
            elif arg.isdigit():
                duration = int(arg)

        print(f"\nConfiguration:")
        print(f"  Rates: {', '.join(f'{r} Hz' for r in rates)}")
        print(f"  Total sampling rate: {sum(rates)} Hz")
        if test_mode:
            print(f"  Mode: TEST (safe mode with reduced rates)")
        if duration:
            print(f"  Duration: {duration} minutes")
        else:
            print(f"  Duration: Continuous (Ctrl+C to stop)")

        if test_mode:
            print("\n⚠ TEST MODE: Running with reduced rates")
            duration = duration or 2  # Default 2 min for test
            print(f"  Auto-duration: {duration} minutes")

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
