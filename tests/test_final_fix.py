#!/usr/bin/env python3
"""
Test final fix: shell=False with list args
"""
import subprocess
import time
import json
from queue import Queue, Empty
import threading

class FixedSensorDaemon:
    def __init__(self, sensor_name, delay_ms=100):
        self.sensor_name = sensor_name
        self.delay_ms = delay_ms
        self.process = None
        self.data_queue = Queue(maxsize=1000)
        self.reader_thread = None
        self.stop_event = threading.Event()

    def start(self):
        # FIX: Use shell=False with list args instead of shell=True
        # This prevents buffering issues
        self.process = subprocess.Popen(
            ['stdbuf', '-oL', 'termux-sensor', '-s', self.sensor_name, '-d', str(self.delay_ms)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )

        self.reader_thread = threading.Thread(target=self._read_stream, daemon=True)
        self.reader_thread.start()
        print(f"✓ Daemon started with sensor: {self.sensor_name}")

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

                    except Exception as e:
                        json_buffer = ""

        except Exception as e:
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
    print("Testing with full sensor name...")
    daemon1 = FixedSensorDaemon('lsm6dso LSM6DSO Accelerometer Non-wakeup', delay_ms=100)
    daemon1.start()
    time.sleep(1)

    print("Reading 5 samples...")
    for i in range(5):
        data = daemon1.get_data(timeout=0.5)
        if data:
            print(f"  Sample {i+1}: x={data['x']:.3f}, y={data['y']:.3f}, z={data['z']:.3f}")
        else:
            print(f"  Sample {i+1}: No data")

    daemon1.stop()

    print("\nTesting with partial match 'accel'...")
    daemon2 = FixedSensorDaemon('accel', delay_ms=100)
    daemon2.start()
    time.sleep(1)

    print("Reading 5 samples...")
    for i in range(5):
        data = daemon2.get_data(timeout=0.5)
        if data:
            print(f"  Sample {i+1}: x={data['x']:.3f}, y={data['y']:.3f}, z={data['z']:.3f}")
        else:
            print(f"  Sample {i+1}: No data")

    daemon2.stop()
    print("\n✓ Test complete")

if __name__ == '__main__':
    main()
