#!/usr/bin/env python3
"""
Debug version of SensorDaemon to see where it's hanging
"""
import subprocess
import threading
import time
import json
from queue import Queue, Empty

class DebugSensorDaemon:
    def __init__(self, sensor_name, delay_ms=100):
        self.sensor_name = sensor_name
        self.delay_ms = delay_ms
        self.process = None
        self.data_queue = Queue(maxsize=1000)
        self.reader_thread = None
        self.stop_event = threading.Event()

    def start(self):
        cmd = f"stdbuf -oL termux-sensor -s '{self.sensor_name}' -d {self.delay_ms}"
        print(f"Command: {cmd}")

        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            shell=True
        )

        self.reader_thread = threading.Thread(target=self._read_stream, daemon=True)
        self.reader_thread.start()
        print("Reader thread started")

    def _read_stream(self):
        print("[DEBUG] _read_stream started")
        if not self.process:
            print("[DEBUG] No process!")
            return

        try:
            json_buffer = ""
            brace_count = 0
            line_count = 0

            print("[DEBUG] Starting to read stdout...")
            for line in self.process.stdout:
                line_count += 1
                print(f"[DEBUG] Line {line_count}: {line[:60]}...")  # First 60 chars

                if self.stop_event.is_set():
                    print("[DEBUG] Stop event set")
                    break

                json_buffer += line
                brace_count += line.count('{') - line.count('}')

                print(f"[DEBUG] Brace count: {brace_count}, buffer len: {len(json_buffer)}")

                if brace_count == 0 and json_buffer.strip():
                    print(f"[DEBUG] Complete JSON detected: {json_buffer[:100]}...")
                    try:
                        data = json.loads(json_buffer)
                        print(f"[DEBUG] Parsed JSON: {data}")

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
                                    print(f"[DEBUG] Putting data in queue: {accel_data}")
                                    self.data_queue.put_nowait(accel_data)

                        json_buffer = ""

                    except Exception as e:
                        print(f"[DEBUG] JSON parse error: {e}")
                        json_buffer = ""

                if line_count >= 20:
                    print("[DEBUG] Stopping after 20 lines for debugging")
                    break

        except Exception as e:
            print(f"[DEBUG] Stream error: {e}")
        finally:
            print("[DEBUG] _read_stream finished")

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
    daemon = DebugSensorDaemon('lsm6dso LSM6DSO Accelerometer Non-wakeup', delay_ms=100)
    daemon.start()

    time.sleep(3)

    print("\n=== Trying to read data ===")
    for i in range(5):
        data = daemon.get_data(timeout=1.0)
        if data:
            print(f"Got data: {data}")
        else:
            print("No data")

    daemon.stop()

if __name__ == '__main__':
    main()
