#!/usr/bin/env python3
"""
Test without shell=True to see if that fixes buffering
"""
import subprocess
import time

# Try with shell=False and argument list
sensor_name = 'lsm6dso LSM6DSO Accelerometer Non-wakeup'
delay_ms = 100

# Method 1: shell=False with list args
print("=== Method 1: shell=False with list args ===")
process1 = subprocess.Popen(
    ['stdbuf', '-oL', 'termux-sensor', '-s', sensor_name, '-d', str(delay_ms)],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    bufsize=1
)

print("Waiting 1 second...")
time.sleep(1)

print("Reading 3 lines...")
for i in range(3):
    line = process1.stdout.readline()
    if line:
        print(f"  Line {i+1}: {line[:60]}")
    else:
        print(f"  Line {i+1}: EMPTY")
        break

process1.terminate()
process1.wait(timeout=2)

print("\n=== Method 2: Partial match 'accel' instead ===")
process2 = subprocess.Popen(
    ['stdbuf', '-oL', 'termux-sensor', '-s', 'accel', '-d', str(delay_ms)],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    bufsize=1
)

print("Waiting 1 second...")
time.sleep(1)

print("Reading 3 lines...")
for i in range(3):
    line = process2.stdout.readline()
    if line:
        print(f"  Line {i+1}: {line[:60]}")
    else:
        print(f"  Line {i+1}: EMPTY")
        break

process2.terminate()
process2.wait(timeout=2)

print("\nDone")
