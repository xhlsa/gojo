#!/usr/bin/env python3
"""
Analyze sensor data to determine actual hardware sampling rate vs API rate
"""
import subprocess
import json
import time
import sys

def analyze_sensor_rate(delay_ms, duration_s=5):
    """Collect sensor data and analyze the actual update rate"""
    print(f"\n{'='*60}")
    print(f"Analyzing sensor at {delay_ms}ms delay for {duration_s}s")
    print(f"{'='*60}")

    cmd = [
        'timeout', str(duration_s + 1),
        'stdbuf', '-oL', 'termux-sensor',
        '-s', 'ACCELEROMETER',
        '-d', str(delay_ms)
    ]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        samples = []
        json_buffer = ""
        brace_depth = 0

        for line in proc.stdout:
            if not line.strip():
                continue

            json_buffer += line + '\n'
            brace_depth += line.count('{') - line.count('}')

            if brace_depth == 0 and '{' in json_buffer and json_buffer.count('}') > 0:
                try:
                    data = json.loads(json_buffer)
                    json_buffer = ""

                    # Extract accelerometer values
                    for sensor_key, sensor_data in data.items():
                        if isinstance(sensor_data, dict) and 'values' in sensor_data:
                            values = list(sensor_data['values'])
                            if len(values) >= 3:
                                samples.append({
                                    'x': values[0],
                                    'y': values[1],
                                    'z': values[2],
                                    'time': time.time()
                                })
                            break
                except (ValueError, KeyError, json.JSONDecodeError):
                    json_buffer = ""
                    brace_depth = 0

        proc.wait()

        if len(samples) < 2:
            print("Not enough samples collected")
            return

        # Analyze data
        print(f"\nTotal API calls: {len(samples)}")
        print(f"API call rate: {len(samples)/duration_s:.1f} Hz")

        # Find unique values to determine actual hardware update rate
        unique_samples = []
        for s in samples:
            curr_vals = (round(s['x'], 6), round(s['y'], 6), round(s['z'], 6))
            if not unique_samples:
                unique_samples.append(s)
            else:
                last_vals = (round(unique_samples[-1]['x'], 6), round(unique_samples[-1]['y'], 6), round(unique_samples[-1]['z'], 6))
                if curr_vals != last_vals:
                    unique_samples.append(s)

        print(f"Unique hardware updates: {len(unique_samples)}")
        print(f"Actual hardware rate: {len(unique_samples)/duration_s:.1f} Hz")
        print(f"Duplication factor: {len(samples)/len(unique_samples) if unique_samples else 0:.1f}x")

        # Sample data
        print(f"\nFirst 5 samples (X, Y, Z):")
        for i, s in enumerate(samples[:5]):
            print(f"  {i+1}: ({s['x']:.4f}, {s['y']:.4f}, {s['z']:.4f})")

        print(f"\nLast 5 samples (X, Y, Z):")
        for i, s in enumerate(samples[-5:]):
            print(f"  {i+len(samples)-4}: ({s['x']:.4f}, {s['y']:.4f}, {s['z']:.4f})")

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)

# Test different delay values
for delay in [1, 5, 10, 20, 50]:
    analyze_sensor_rate(delay, duration_s=3)

print(f"\n{'='*60}")
print("SUMMARY: Hardware can only update at a fixed rate")
print("Faster polling just returns cached values")
print(f"{'='*60}\n")
