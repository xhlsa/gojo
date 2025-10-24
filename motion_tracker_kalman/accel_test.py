#!/usr/bin/env python3
"""
Quick accelerometer diagnostic - tests if SensorDaemon is working at all
by reading raw data and showing live values while you shake the phone
"""

import subprocess
import json
import time
import sys
from queue import Queue, Empty
import threading

class SensorDaemon:
    def __init__(self, sensor_type='accelerometer', delay_ms=20, max_queue_size=1000):
        self.sensor_type = sensor_type
        self.delay_ms = delay_ms
        self.process = None
        self.data_queue = Queue(maxsize=max_queue_size)
        self.reader_thread = None
        self.stop_event = threading.Event()

    def start(self):
        try:
            # Map generic sensor names to actual device sensor names
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

# Test it
print("\n" + "="*80)
print("ACCELEROMETER DIAGNOSTIC TEST")
print("="*80)
print("\nShake your phone around for ~10 seconds...")
print("Looking for acceleration values...\n")

daemon = SensorDaemon()
if not daemon.start():
    print("Failed to start sensor daemon!")
    sys.exit(1)

time.sleep(1)

sample_count = 0
max_magnitude = 0
start_time = time.time()

try:
    while time.time() - start_time < 15:  # 15 second test
        data = daemon.get_data(timeout=0.2)
        if data:
            magnitude = (data['x']**2 + data['y']**2 + data['z']**2)**0.5
            max_magnitude = max(max_magnitude, magnitude)
            sample_count += 1
            
            # Show every 10th sample
            if sample_count % 10 == 0:
                print(f"Sample {sample_count}: x={data['x']:7.2f} y={data['y']:7.2f} z={data['z']:7.2f} mag={magnitude:7.2f}")
        else:
            print(".", end="", flush=True)
            
except KeyboardInterrupt:
    print("\n\nStopped by user")
finally:
    daemon.stop()

print("\n" + "="*80)
print(f"RESULTS")
print("="*80)
print(f"Samples collected: {sample_count}")
print(f"Max magnitude: {max_magnitude:.2f} m/s²")

if sample_count == 0:
    print("\n⚠️ NO SAMPLES COLLECTED - Sensor daemon not working!")
    print("   Try: termux-sensor -s accelerometer -d 20")
elif sample_count < 50:
    print(f"\n⚠️ LOW SAMPLE COUNT - Only {sample_count} in 15 seconds (expected ~750)")
    print("   Sensor daemon may be slow or blocked")
else:
    print(f"\n✓ GOOD - Sensor daemon working ({sample_count} samples = {sample_count/15:.0f} Hz)")
    print(f"  Max motion detected: {max_magnitude:.2f} m/s²")

print("="*80 + "\n")
