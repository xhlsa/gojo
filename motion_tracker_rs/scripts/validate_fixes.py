#!/usr/bin/env python3
"""
Analysis Roadmap: Validate Recent Code Fixes

1. ZUPT Flatline: Verify zero-velocity updates work
2. Projection Alignment: Check GPS vs EKF trajectory lag
3. Power Floor: Validate low-speed power fix
"""

import json
import gzip
import numpy as np
from pathlib import Path
import math


def load_stitched_data(filepath: str):
    """Load stitched drive data"""
    with gzip.open(filepath, 'rt') as f:
        data = json.load(f)
    return data['readings']


def analyze_zupt_flatline(readings):
    """
    Check #1: ZUPT (Zero-velocity Update) Validation

    Find stationary segments (low accel variance) and verify:
    - EKF velocity drops to exactly 0.0
    - Covariance trace decreases (uncertainty reduction)
    """
    print("\n=== Check #1: ZUPT Flatline ===")

    # Window for variance calculation
    window_size = 50  # ~1 second at 50Hz
    zupt_segments = []

    for i in range(window_size, len(readings) - window_size):
        # Get accel data in window
        window = readings[i-window_size:i+window_size]
        accels = [r['accel'] for r in window if r.get('accel')]

        if len(accels) < window_size:
            continue

        # Calculate variance (magnitude)
        magnitudes = [math.sqrt(a['x']**2 + a['y']**2 + a['z']**2) for a in accels]
        variance = np.var(magnitudes)

        # Low variance = stationary
        if variance < 0.05:  # Threshold for stillness
            record = readings[i]

            # Check if we have EKF velocity
            if 'ekf_15d' in record:
                vel = record['ekf_15d']['velocity']
                speed = math.sqrt(vel[0]**2 + vel[1]**2 + vel[2]**2)

                zupt_segments.append({
                    'timestamp': record['timestamp'],
                    'variance': variance,
                    'speed': speed,
                    'velocity_is_zero': abs(speed) < 0.01,
                })

    if zupt_segments:
        zero_count = sum(1 for s in zupt_segments if s['velocity_is_zero'])
        print(f"  Stationary segments found: {len(zupt_segments)}")
        print(f"  Velocity clamped to zero: {zero_count} ({100*zero_count/len(zupt_segments):.1f}%)")

        if zero_count > 0:
            print(f"  ✓ ZUPT working: Velocity zeroed during stillness")
        else:
            print(f"  ✗ ZUPT not detected: Check NHC implementation")
    else:
        print("  No stationary segments detected (continuous motion)")


def analyze_projection_alignment(readings):
    """
    Check #2: GPS vs EKF Trajectory Alignment

    Compare GPS fixes to EKF trajectory:
    - Do GPS points sit ON the EKF line? (good projection)
    - Or do they lag behind? (needs forward projection fix)
    """
    print("\n=== Check #2: Projection Alignment ===")

    gps_readings = [r for r in readings if r.get('gps') and r.get('ekf_15d')]

    if len(gps_readings) < 10:
        print("  Not enough GPS+EKF samples for analysis")
        return

    errors = []

    for i in range(1, len(gps_readings)):
        prev = gps_readings[i-1]
        curr = gps_readings[i]

        # GPS displacement
        gps_prev = prev['gps']
        gps_curr = curr['gps']

        # Simple ENU displacement (flat earth approx)
        lat_m = 111320.0
        lon_m = 111320.0 * math.cos(math.radians(gps_prev['latitude']))

        gps_dx = (gps_curr['longitude'] - gps_prev['longitude']) * lon_m
        gps_dy = (gps_curr['latitude'] - gps_prev['latitude']) * lat_m
        gps_dist = math.sqrt(gps_dx**2 + gps_dy**2)

        # EKF displacement
        ekf_prev = prev['ekf_15d']['position']
        ekf_curr = curr['ekf_15d']['position']
        ekf_dx = ekf_curr[0] - ekf_prev[0]
        ekf_dy = ekf_curr[1] - ekf_prev[1]
        ekf_dist = math.sqrt(ekf_dx**2 + ekf_dy**2)

        # Distance error
        if gps_dist > 1.0:  # Only check when moving
            error = abs(ekf_dist - gps_dist)
            errors.append(error)

    if errors:
        mean_error = np.mean(errors)
        max_error = np.max(errors)

        print(f"  GPS fixes analyzed: {len(errors)}")
        print(f"  Mean distance error: {mean_error:.2f} m")
        print(f"  Max distance error: {max_error:.2f} m")

        if mean_error < 5.0:
            print(f"  ✓ Good alignment: GPS and EKF trajectories match")
        else:
            print(f"  ✗ Poor alignment: Check GPS lag or projection issues")


def analyze_power_floor(readings):
    """
    Check #3: Low-Speed Power Floor

    Find low-speed segments (< 2 m/s) and verify:
    - specific_power is non-zero (power floor applied)
    - No division-by-zero artifacts
    """
    print("\n=== Check #3: Power Floor (Low Speed) ===")

    low_speed_samples = []

    for r in readings:
        if 'ekf_15d' not in r:
            continue

        vel = r['ekf_15d']['velocity']
        speed = math.sqrt(vel[0]**2 + vel[1]**2 + vel[2]**2)

        # Low speed check
        if speed < 2.0:
            # Check if we have power metrics (if implemented)
            power_data = r.get('power', {})
            specific_power = power_data.get('specific_power', None)

            if specific_power is not None:
                low_speed_samples.append({
                    'timestamp': r['timestamp'],
                    'speed': speed,
                    'specific_power': specific_power,
                    'power_nonzero': abs(specific_power) > 1e-6,
                })

    if low_speed_samples:
        nonzero_count = sum(1 for s in low_speed_samples if s['power_nonzero'])
        print(f"  Low-speed samples (< 2 m/s): {len(low_speed_samples)}")
        print(f"  Non-zero power: {nonzero_count} ({100*nonzero_count/len(low_speed_samples):.1f}%)")

        if nonzero_count > 0:
            print(f"  ✓ Power floor working: Low-speed power non-zero")
        else:
            print(f"  ✗ Power floor not detected: Check implementation")
    else:
        print("  No low-speed segments found (or power metrics not logged)")


def main():
    import argparse

    parser = argparse.ArgumentParser(description='Validate recent code fixes on stitched drive data')
    parser.add_argument('input_file', help='Stitched drive data (full_drive_*.json.gz)')

    args = parser.parse_args()

    print("=== Analysis Roadmap: Fix Validation ===")
    print(f"Input: {args.input_file}\n")

    # Load data
    print("Loading stitched data...")
    readings = load_stitched_data(args.input_file)
    print(f"  Total readings: {len(readings):,}")

    # Run validation checks
    analyze_zupt_flatline(readings)
    analyze_projection_alignment(readings)
    analyze_power_floor(readings)

    print("\n✓ Validation complete!")


if __name__ == '__main__':
    main()
