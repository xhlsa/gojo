#!/usr/bin/env python3
"""
Ultra-Sensitive Accelerometer Test - Detects micro-vibrations and phone shakes
Perfect for validating Kalman filter sensitivity indoors
Run and shake your phone in different directions while the test runs
"""

import subprocess
import json
import time
import sys
from queue import Queue, Empty
import threading
from collections import deque
from statistics import mean, stdev

class SensorDaemon:
    """Exact copy from motion_tracker_v2.py - proven working"""
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
                                    accel_data = {
                                        'x': values[0],
                                        'y': values[1],
                                        'z': values[2],
                                        'timestamp': time.time()
                                    }
                                    try:
                                        self.data_queue.put_nowait(accel_data)
                                    except:
                                        pass
                        json_buffer = ""
                    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                        json_buffer = ""
        except Exception as e:
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


print("\n" + "="*80)
print("ULTRA-SENSITIVE ACCELEROMETER TEST - Kalman Validation")
print("="*80)
print("\nPhase 1: CALIBRATION (3 seconds - keep phone STILL)")
print("This establishes baseline noise floor...\n")

daemon = SensorDaemon(delay_ms=20)  # 50Hz - proven working config
if not daemon.start():
    print("Failed to start sensor daemon!")
    sys.exit(1)

time.sleep(0.5)

# ============================================================================
# PHASE 1: CALIBRATION - Measure baseline noise
# ============================================================================
baseline_samples = deque(maxlen=300)  # 3 seconds @ 100Hz
cal_start = time.time()

while time.time() - cal_start < 3:
    data = daemon.get_data(timeout=0.1)
    if data:
        magnitude = (data['x']**2 + data['y']**2 + data['z']**2)**0.5
        baseline_samples.append(magnitude)
        print(".", end="", flush=True)

print("\n")

if len(baseline_samples) < 50:
    print(f"\n⚠ WARNING: Only {len(baseline_samples)} baseline samples (expected ~300)")
    print("Sensor may be too slow. Check: termux-sensor -s accelerometer -d 10")
    daemon.stop()
    sys.exit(1)

baseline_mean = mean(baseline_samples)
baseline_stdev = stdev(baseline_samples) if len(baseline_samples) > 1 else 0
baseline_min = min(baseline_samples)
baseline_max = max(baseline_samples)

print(f"✓ Baseline established:")
print(f"  Mean: {baseline_mean:.3f} m/s²")
print(f"  StdDev: {baseline_stdev:.4f} m/s²")
print(f"  Range: {baseline_min:.3f} to {baseline_max:.3f} m/s²")
print(f"  Samples collected: {len(baseline_samples)}")

# ============================================================================
# PHASE 2: SENSITIVITY TEST - Shake phone while detecting events
# ============================================================================
print("\n" + "="*80)
print("Phase 2: SENSITIVITY TEST (15 seconds - SHAKE YOUR PHONE NOW!)")
print("="*80)
print("Detecting micro-vibrations, taps, and movements...\n")

motion_threshold = baseline_mean + (3 * baseline_stdev)  # 3-sigma threshold
event_samples = deque(maxlen=1500)  # 15 seconds @ 100Hz
event_count = 0
peak_magnitude = baseline_mean
peak_event_mag = 0
event_window = deque(maxlen=10)  # Track recent events
last_event_time = 0

test_start = time.time()

try:
    while time.time() - test_start < 15:
        data = daemon.get_data(timeout=0.05)
        if data:
            magnitude = (data['x']**2 + data['y']**2 + data['z']**2)**0.5
            event_samples.append(magnitude)
            peak_magnitude = max(peak_magnitude, magnitude)

            # Event detection: magnitude exceeds 3-sigma baseline
            is_event = magnitude > motion_threshold

            if is_event:
                current_time = time.time()
                time_since_last = current_time - last_event_time

                # Group events within 100ms as single motion event
                if time_since_last > 0.1:
                    event_count += 1
                    peak_event_mag = magnitude
                    last_event_time = current_time
                else:
                    peak_event_mag = max(peak_event_mag, magnitude)

                # Real-time feedback
                event_mag = magnitude - baseline_mean
                print(f"[EVENT {event_count}] {current_time - test_start:6.2f}s | "
                      f"Mag: {magnitude:7.3f} m/s² | "
                      f"Δ from baseline: {event_mag:+7.3f} m/s² | "
                      f"X:{data['x']:7.2f} Y:{data['y']:7.2f} Z:{data['z']:7.2f}")
        else:
            print(".", end="", flush=True)

except KeyboardInterrupt:
    print("\n\nStopped by user")
finally:
    daemon.stop()

# ============================================================================
# PHASE 3: ANALYSIS & RESULTS
# ============================================================================
print("\n" + "="*80)
print("RESULTS & ANALYSIS")
print("="*80)

print(f"\nBaseline Noise (3 second still reading):")
print(f"  Mean magnitude: {baseline_mean:.4f} m/s²")
print(f"  Std dev: {baseline_stdev:.4f} m/s²")
print(f"  Min/Max: {baseline_min:.4f} / {baseline_max:.4f} m/s²")

print(f"\nMotion Detection Threshold: {motion_threshold:.4f} m/s² (mean + 3σ)")

if len(event_samples) > 0:
    event_mean = mean(event_samples)
    event_max = max(event_samples)
    event_min = min(event_samples)

    print(f"\nMotion Test Results:")
    print(f"  Total samples: {len(event_samples)}")
    print(f"  Events detected: {event_count}")
    print(f"  Peak magnitude: {event_max:.4f} m/s²")
    print(f"  Peak Δ from baseline: {(event_max - baseline_mean):.4f} m/s²")
    print(f"  Test mean (overall): {event_mean:.4f} m/s²")

    sensitivity_ratio = peak_magnitude / baseline_mean if baseline_mean > 0 else 0
    print(f"\nSensitivity Analysis:")
    print(f"  Peak/Baseline ratio: {sensitivity_ratio:.2f}x")
    print(f"  Noise floor: {baseline_stdev:.4f} m/s² (can detect ~{3*baseline_stdev:.4f} m/s²)")

    if event_count == 0:
        print(f"\n⚠️ NO EVENTS DETECTED")
        print(f"   Threshold may be too high: {motion_threshold:.4f} m/s²")
        print(f"   Try stronger shaking or adjust 3-sigma threshold")
    elif event_count < 5:
        print(f"\n⚠️ LOW EVENT COUNT: Only {event_count} events detected")
        print(f"   Sensor sensitivity seems low for this test")
    else:
        print(f"\n✓ GOOD SENSITIVITY: {event_count} distinct motion events detected")
        print(f"   Kalman filter has {event_count} clear motion signatures to work with")
else:
    print(f"\n⚠️ NO SAMPLES COLLECTED DURING TEST")
    print(f"   Sensor daemon may have crashed")

print("\n" + "="*80)
print("Tips for Ultra-Sensitive Testing:")
print("  1. Hold phone loosely and shake in random directions")
print("  2. Tap the phone gently (micro-vibrations)")
print("  3. Rotate phone quickly (detects rotational acceleration)")
print("  4. Walk with phone in hand (motion + gravity)")
print("="*80 + "\n")
