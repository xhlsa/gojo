#!/usr/bin/env python3
"""
[LEGACY] Simple accelerometer reader for Termux using termux-sensor.

⚠️ DEPRECATED: Use accel_reader.sh instead
This Python version had issues handling termux-sensor's daemon process.
See ACCEL_FINDINGS.md for technical details on why bash works better.

Original purpose: Wake the sensor and read accelerometer data.
"""

import subprocess
import json
import time
import sys
from pathlib import Path


def wake_sensor():
    """Wake the accelerometer sensor by reading it once."""
    try:
        # termux-sensor runs continuously, so we need to timeout it
        subprocess.run(
            ["termux-sensor", "-s", "ACCELEROMETER"],
            capture_output=True,
            timeout=1
        )
    except subprocess.TimeoutExpired:
        # Expected - sensor runs continuously
        pass
    except Exception as e:
        print(f"Error waking sensor: {e}", file=sys.stderr)
        return False

    time.sleep(0.3)  # Brief pause for sensor to initialize
    return True


def read_accel(raw=False):
    """
    Read accelerometer data.

    Args:
        raw: If True, return raw JSON. If False, parse and format nicely.

    Returns:
        dict with accel data or None on error
    """
    try:
        # termux-sensor outputs JSON once, then continues running
        # Use shell command with timeout and head to get just the first JSON output
        result = subprocess.run(
            "termux-sensor -s ACCELEROMETER 2>&1 | head -20",
            shell=True,
            capture_output=True,
            text=True,
            timeout=3
        )

        output = result.stdout or result.stderr

    except subprocess.TimeoutExpired:
        print("Error: sensor read timeout", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Error reading sensor: {e}", file=sys.stderr)
        return None

    if not output:
        print("Error: no sensor output", file=sys.stderr)
        return None

    try:
        # Parse JSON output - extract just the JSON part
        output = output.strip()

        # Handle case where there's extra text before/after JSON
        json_start = output.find('{')
        json_end = output.rfind('}')

        if json_start != -1 and json_end != -1 and json_end > json_start:
            output = output[json_start:json_end + 1]

        data = json.loads(output)

        if raw:
            return data

        # Extract and format nicely
        # Format: {"sensor_name": {"values": [x, y, z], ...}}
        if data and isinstance(data, dict):
            # Get first sensor in dict
            for sensor_name, sensor_data in data.items():
                values = sensor_data.get('values', [])
                if len(values) >= 3:
                    return {
                        'x': values[0],
                        'y': values[1],
                        'z': values[2],
                        'accuracy': sensor_data.get('accuracy'),
                        'timestamp': sensor_data.get('timestamp'),
                    }

        return None

    except json.JSONDecodeError as e:
        print(f"Error parsing sensor output: {e}", file=sys.stderr)
        return None


def continuous_read(duration=10, interval=0.5):
    """
    Read accelerometer continuously for a duration.

    Args:
        duration: How long to read (seconds)
        interval: Time between reads (seconds)
    """
    print(f"Reading accelerometer for {duration}s (interval: {interval}s)")
    print("-" * 60)

    start = time.time()
    count = 0

    while time.time() - start < duration:
        data = read_accel()
        if data:
            count += 1
            elapsed = time.time() - start
            print(f"[{elapsed:6.2f}s] X:{data['x']:+7.2f} Y:{data['y']:+7.2f} Z:{data['z']:+7.2f} m/s²")

        time.sleep(interval)

    print("-" * 60)
    print(f"Total reads: {count}")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description='Simple Termux accelerometer reader'
    )
    parser.add_argument(
        '-c', '--continuous',
        type=float,
        default=0,
        metavar='DURATION',
        help='Read continuously for N seconds (default: single read)'
    )
    parser.add_argument(
        '-i', '--interval',
        type=float,
        default=0.5,
        metavar='SECONDS',
        help='Time between reads in continuous mode (default: 0.5s)'
    )
    parser.add_argument(
        '-r', '--raw',
        action='store_true',
        help='Output raw JSON instead of formatted values'
    )
    parser.add_argument(
        '-w', '--wake-only',
        action='store_true',
        help='Only wake the sensor, do not read'
    )

    args = parser.parse_args()

    # Wake sensor
    print("Waking accelerometer sensor...", file=sys.stderr)
    if not wake_sensor():
        sys.exit(1)

    if args.wake_only:
        print("Sensor ready", file=sys.stderr)
        return

    # Read mode
    if args.continuous > 0:
        continuous_read(duration=args.continuous, interval=args.interval)
    else:
        # Single read
        data = read_accel(raw=args.raw)
        if data:
            if args.raw:
                print(json.dumps(data, indent=2))
            else:
                print(f"X: {data['x']:+.2f} m/s²")
                print(f"Y: {data['y']:+.2f} m/s²")
                print(f"Z: {data['z']:+.2f} m/s²")
                if data['accuracy'] is not None:
                    print(f"Accuracy: {data['accuracy']}")
        else:
            sys.exit(1)


if __name__ == '__main__':
    main()
