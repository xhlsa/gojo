#!/usr/bin/env python3
"""
Minimal sensor daemon debug test
Check if termux-sensor actually produces output with the awaken() detected sensor
"""

import subprocess
import json
import time
import sys

def get_sensor_name():
    """Get actual sensor name using awaken() logic"""
    try:
        output = subprocess.check_output(
            "termux-sensor -l",
            shell=True,
            text=True,
            timeout=5
        )

        # Parse JSON
        try:
            sensor_data = json.loads(output)
            available_sensors = sensor_data.get('sensors', [])
        except json.JSONDecodeError:
            available_sensors = [line.strip() for line in output.strip().split('\n') if line.strip()]

        print(f"[INFO] Found {len(available_sensors)} sensors")

        # Filter for accelerometer
        matching = [s for s in available_sensors if 'accel' in s.lower()]
        print(f"[INFO] Matching 'accel': {matching}")

        # Filter out derived sensors
        raw = [s for s in matching if 'linear_acceleration' not in s.lower() and 'uncalibrated' not in s.lower()]
        print(f"[INFO] Raw sensors: {raw}")

        if raw:
            sensor_name = raw[0]
            print(f"[OK] Selected: {sensor_name}")
            return sensor_name

        return None
    except Exception as e:
        print(f"[ERROR] {e}")
        return None

def test_raw_output():
    """Test raw termux-sensor output without shell"""
    sensor_name = get_sensor_name()
    if not sensor_name:
        print("[FATAL] Could not detect sensor")
        return

    print("\n" + "="*60)
    print("Testing raw termux-sensor output (no shell, 5 sec)")
    print("="*60)

    try:
        # Test 1: Direct call without stdbuf
        print("\n[TEST 1] Direct call: termux-sensor -s SENSOR -d 20 -n 5")
        proc = subprocess.Popen(
            ['termux-sensor', '-s', sensor_name, '-d', '20', '-n', '5'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )

        line_count = 0
        json_count = 0

        for line in proc.stdout:
            line_count += 1
            print(f"  Line {line_count}: {line[:60]}...")
            if '{' in line:
                json_count += 1

        proc.wait(timeout=10)
        print(f"[RESULT] {line_count} lines, {json_count} JSON lines")

    except Exception as e:
        print(f"[ERROR] {e}")
        return

    # Test 2: With stdbuf
    print("\n[TEST 2] With stdbuf: stdbuf -oL termux-sensor -s SENSOR -d 20 -n 5")
    try:
        proc = subprocess.Popen(
            ['stdbuf', '-oL', 'termux-sensor', '-s', sensor_name, '-d', '20', '-n', '5'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )

        line_count = 0
        json_count = 0

        for line in proc.stdout:
            line_count += 1
            print(f"  Line {line_count}: {line[:60]}...")
            if '{' in line:
                json_count += 1

        proc.wait(timeout=10)
        print(f"[RESULT] {line_count} lines, {json_count} JSON lines")

    except Exception as e:
        print(f"[ERROR] {e}")

def test_brace_counting():
    """Test the brace-counting JSON parser"""
    sensor_name = get_sensor_name()
    if not sensor_name:
        print("[FATAL] Could not detect sensor")
        return

    print("\n" + "="*60)
    print("Testing brace-counting parser (10 sec, -d 100)")
    print("="*60)

    try:
        proc = subprocess.Popen(
            ['termux-sensor', '-s', sensor_name, '-d', '100'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )

        buffer = ""
        brace_count = 0
        json_count = 0
        start_time = time.time()

        while time.time() - start_time < 10:
            line = proc.stdout.readline()
            if not line:
                break

            buffer += line
            brace_count += line.count('{') - line.count('}')

            if brace_count == 0 and buffer.strip():
                try:
                    data = json.loads(buffer)
                    json_count += 1

                    # Extract sensor name and values
                    sensor_key = list(data.keys())[0]
                    values = data[sensor_key].get('values', [])

                    print(f"  Sample {json_count}: {sensor_key[:30]}... = [{values[0]:.3f}, {values[1]:.3f}, {values[2]:.3f}]")

                    buffer = ""
                except json.JSONDecodeError as e:
                    print(f"  [PARSE ERROR] {str(e)[:40]}")
                    buffer = ""

        proc.terminate()
        print(f"\n[RESULT] Parsed {json_count} complete JSON objects in 10 seconds")
        print(f"[RATE] {json_count / 10:.1f} Hz")

    except Exception as e:
        print(f"[ERROR] {e}")

if __name__ == "__main__":
    test_raw_output()
    test_brace_counting()
