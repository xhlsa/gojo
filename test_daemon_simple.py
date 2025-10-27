#!/usr/bin/env python3
"""
Simple daemon diagnostic script to test termux-sensor subprocess
"""
import subprocess
import json
import time
import threading
from queue import Queue, Empty

print("=" * 80)
print("DAEMON SUBPROCESS TEST - stdbuf -oL approach")
print("=" * 80)

# Test 1: Direct termux-sensor with stdbuf
print("\n[TEST 1] Testing termux-sensor with stdbuf -oL (non-blocking)")
try:
    proc = subprocess.Popen(
        ['stdbuf', '-oL', 'termux-sensor', '-s', 'lsm6dso LSM6DSO Accelerometer Non-wakeup', '-d', '20'],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1
    )
    print(f"✓ Process started (PID: {proc.pid})")

    json_buffer = ""
    brace_count = 0
    sample_count = 0
    start_time = time.time()

    for line in proc.stdout:
        json_buffer += line
        brace_count += line.count('{') - line.count('}')

        if brace_count == 0 and json_buffer.strip():
            try:
                data = json.loads(json_buffer)
                sample_count += 1
                if sample_count % 10 == 1:
                    print(f"  Sample {sample_count}: {list(data.keys())}")
                json_buffer = ""
            except:
                json_buffer = ""

        # Read for 5 seconds
        if time.time() - start_time > 5:
            break

    proc.terminate()
    elapsed = time.time() - start_time
    rate = sample_count / elapsed if elapsed > 0 else 0
    print(f"✓ Collected {sample_count} samples in {elapsed:.1f}s ({rate:.1f} Hz)")

except Exception as e:
    print(f"✗ Error: {e}")

print("\n" + "=" * 80)
