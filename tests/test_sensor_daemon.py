#!/usr/bin/env python3
"""
Minimal test script for SensorDaemon debugging
"""
import time
import sys
sys.path.insert(0, '/data/data/com.termux/files/home/gojo/motion_tracker_v2')
from motion_tracker_v2 import SensorDaemon

def test_sensor_daemon():
    print("Creating SensorDaemon...")
    daemon = SensorDaemon(sensor_type='accelerometer', delay_ms=100)

    print("Calling awaken()...")
    if not daemon.awaken():
        print("❌ awaken() failed")
        return

    print(f"✓ Sensor found: {daemon.actual_sensor_name}")

    print("Calling start()...")
    if not daemon.start():
        print("❌ start() failed")
        return

    print("✓ Daemon started")

    # Wait a bit for process to initialize
    print("Waiting 2 seconds for data...")
    time.sleep(2)

    # Try to read data
    print("Attempting to read data (5 samples)...")
    for i in range(5):
        data = daemon.get_data(timeout=1.0)
        if data:
            print(f"  Sample {i+1}: x={data['x']:.3f}, y={data['y']:.3f}, z={data['z']:.3f}")
        else:
            print(f"  Sample {i+1}: No data (timeout)")

    # Check if process is alive
    if daemon.process:
        print(f"Process alive: {daemon.process.poll() is None}")
        if daemon.process.poll() is not None:
            print(f"Process exit code: {daemon.process.poll()}")
            # Read stderr
            stderr_output = daemon.process.stderr.read()
            if stderr_output:
                print(f"Stderr: {stderr_output}")

    print("Stopping daemon...")
    daemon.stop()
    print("✓ Test complete")

if __name__ == '__main__':
    test_sensor_daemon()
