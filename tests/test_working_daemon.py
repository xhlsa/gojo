#!/usr/bin/env python3
"""
Final working SensorDaemon with all fixes applied
"""
import subprocess
import time
import json
from queue import Queue, Empty
import threading

class WorkingSensorDaemon:
    def __init__(self, sensor_pattern, delay_ms=100):
        """
        sensor_pattern: Partial match pattern for sensor name
                       Use "Accelerometer" for raw accelerometer (case-sensitive!)
                       Use "accel" for linear_acceleration (derived sensor)
        """
        self.sensor_pattern = sensor_pattern
        self.delay_ms = delay_ms
        self.process = None
        self.data_queue = Queue(maxsize=1000)
        self.reader_thread = None
        self.stop_event = threading.Event()

    def start(self):
        """Start sensor daemon - FIXED VERSION"""
        # FIX 1: Use shell=False with list args to avoid buffering issues
        # FIX 2: Use partial match "Accelerometer" (capital A) not "accel"
        self.process = subprocess.Popen(
            ['stdbuf', '-oL', 'termux-sensor', '-s', self.sensor_pattern, '-d', str(self.delay_ms)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )

        self.reader_thread = threading.Thread(target=self._read_stream, daemon=True)
        self.reader_thread.start()
        print(f"✓ Daemon started (pattern='{self.sensor_pattern}', delay={self.delay_ms}ms)")

    def _read_stream(self):
        """Read JSON stream - handles initial {} and multi-line JSON"""
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

                # Complete JSON object detected
                if brace_count == 0 and json_buffer.strip():
                    try:
                        data = json.loads(json_buffer)

                        # Skip empty {} objects (first line from termux-sensor)
                        if not data:
                            json_buffer = ""
                            continue

                        # Extract sensor values
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
                                        pass  # Queue full, skip

                        json_buffer = ""

                    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                        json_buffer = ""

        except Exception:
            pass

    def get_data(self, timeout=None):
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
                self.process.kill()

def main():
    print("=== Testing SensorDaemon with 'Accelerometer' pattern ===")
    daemon = WorkingSensorDaemon('Accelerometer', delay_ms=100)
    daemon.start()

    print("Waiting 2 seconds for data to flow...")
    time.sleep(2)

    print("Reading 10 samples...")
    success_count = 0
    for i in range(10):
        data = daemon.get_data(timeout=0.5)
        if data:
            print(f"  Sample {i+1}: x={data['x']:7.3f}, y={data['y']:7.3f}, z={data['z']:7.3f}")
            success_count += 1
        else:
            print(f"  Sample {i+1}: No data")

    daemon.stop()

    if success_count >= 8:
        print(f"\n✓ SUCCESS: Got {success_count}/10 samples")
    else:
        print(f"\n❌ FAILED: Only got {success_count}/10 samples")

if __name__ == '__main__':
    main()
