#!/usr/bin/env python3
"""
Test raw readline() to see if data flows
"""
import subprocess
import time

cmd = "stdbuf -oL termux-sensor -s 'lsm6dso LSM6DSO Accelerometer Non-wakeup' -d 100"
print(f"Command: {cmd}")

process = subprocess.Popen(
    cmd,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    bufsize=1,
    shell=True
)

print("Process started, waiting 1 second...")
time.sleep(1)

print("Attempting to read lines...")
for i in range(10):
    print(f"Calling readline() #{i+1}...")
    line = process.stdout.readline()
    if line:
        print(f"  Got: {line[:80]}")
    else:
        print("  Got empty line")

    if i == 5:
        print("Breaking after 5 attempts")
        break

print("Terminating process...")
process.terminate()
process.wait(timeout=2)
print("Done")
