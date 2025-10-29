"""
Filter comparison script - benchmarks all available filters.

Compares Complementary, Kalman, EKF, and UKF filters on synthetic or real data.
Measures accuracy, performance, and numerical stability.

Usage:
    python3 compare_filters.py [--synthetic] [--session-file]
    python3 compare_filters.py --synthetic               # Use generated test data
    python3 compare_filters.py --session-file data.json  # Use real motion tracker data
"""

import json
import math
import time
import numpy as np
import argparse
from pathlib import Path
from collections import namedtuple

# Try to import all filters
from . import get_filter

# Synthetic data point
DataPoint = namedtuple('DataPoint', ['timestamp', 'lat', 'lon', 'gps_speed', 'gps_accuracy', 'accel_magnitude'])


def generate_synthetic_data(duration_seconds=300, gps_interval=1.0, accel_rate=50):
    """
    Generate synthetic test data simulating a vehicle drive.

    Includes:
    - GPS points with realistic noise
    - Accelerometer readings at high frequency
    - Periods of acceleration, cruising, and stationary
    """
    data = []
    t = 0
    timestamp = time.time()

    # Simulate vehicle motion: accelerate, cruise, decelerate, stationary
    position = [0.0, 0.0]  # meters from origin
    velocity = 0.0  # m/s
    origin_lat, origin_lon = 37.7749, -122.4194  # SF reference

    # Phase: 0=accel, 1=cruise, 2=decel, 3=stop
    phase = 0
    phase_time = 0
    target_velocity = 0.0

    dt_accel = 1.0 / accel_rate  # ~20ms for 50Hz

    while t < duration_seconds:
        # Phase transitions
        if phase == 0 and phase_time > 60:  # Accelerate for 60s
            phase = 1
            phase_time = 0
            target_velocity = 15.0  # m/s (~54 km/h)
        elif phase == 1 and phase_time > 120:  # Cruise for 120s
            phase = 2
            phase_time = 0
            target_velocity = 0.0
        elif phase == 2 and phase_time > 60:  # Decelerate for 60s
            phase = 3
            phase_time = 0
        elif phase == 3 and phase_time > 60:  # Stop for 60s
            break

        # Velocity dynamics
        if phase == 0:
            accel = 0.5  # m/s²
        elif phase == 1:
            accel = 0.1 * (target_velocity - velocity)  # Damped approach
        elif phase == 2:
            accel = -0.3  # m/s²
        else:
            accel = 0.0
            velocity = 0.0

        velocity += accel * dt_accel
        velocity = max(0, velocity)
        position[0] += velocity * dt_accel

        # Add GPS noise (lower frequency)
        if int(t / dt_accel) % int(gps_interval / dt_accel) == 0:
            # GPS noise
            gps_noise = np.random.normal(0, 3.0, 2)  # 3m std dev

            # Convert position to lat/lon
            R = 6371000
            lat = origin_lat + math.degrees(position[1] / R)
            lon = origin_lon + math.degrees(position[0] / (R * math.cos(math.radians(origin_lat))))
            lat += gps_noise[0] / 111000  # ~111km per degree latitude
            lon += gps_noise[1] / 111000

            gps_speed = velocity + np.random.normal(0, 0.2)  # GPS speed with noise
            gps_accuracy = 5.0 + np.random.normal(0, 1.0)  # 5m ± 1m accuracy

            dp = DataPoint(
                timestamp=timestamp + t,
                lat=lat,
                lon=lon,
                gps_speed=max(0, gps_speed),
                gps_accuracy=max(1.0, gps_accuracy),
                accel_magnitude=0.0  # Will add accel separately
            )
            data.append(dp)

        # Add accelerometer readings (high frequency)
        # Accel magnitude = projection of current acceleration onto motion direction
        if velocity > 0.1:
            accel_mag = abs(accel) + np.random.normal(0, 0.1)
        else:
            accel_mag = np.random.normal(0, 0.05)  # Just noise when stopped

        dp_accel = DataPoint(
            timestamp=timestamp + t,
            lat=None,
            lon=None,
            gps_speed=None,
            gps_accuracy=None,
            accel_magnitude=max(0, accel_mag)
        )
        data.append(dp_accel)

        t += dt_accel
        phase_time += dt_accel

    return data


def load_session_data(json_file):
    """Load motion tracker session data from JSON file."""
    with open(json_file, 'r') as f:
        session = json.load(f)

    data = []

    # Process GPS points
    for gps_point in session.get('gps_data', []):
        dp = DataPoint(
            timestamp=gps_point.get('timestamp', 0),
            lat=gps_point.get('latitude'),
            lon=gps_point.get('longitude'),
            gps_speed=gps_point.get('speed'),
            gps_accuracy=gps_point.get('accuracy'),
            accel_magnitude=0.0
        )
        data.append(dp)

    # Process accelerometer samples
    for accel_sample in session.get('accel_samples', []):
        dp = DataPoint(
            timestamp=accel_sample.get('timestamp', 0),
            lat=None,
            lon=None,
            gps_speed=None,
            gps_accuracy=None,
            accel_magnitude=accel_sample.get('magnitude', 0.0)
        )
        data.append(dp)

    # Sort by timestamp
    data.sort(key=lambda x: x.timestamp)
    return data


def run_filter_on_data(filter_name, data):
    """
    Run a single filter on test data and collect metrics.

    Returns:
        dict with 'name', 'velocities', 'distances', 'times', 'errors'
    """
    try:
        # Create filter instance
        if filter_name == 'kalman':
            # Kalman requires filterpy
            try:
                filter_obj = get_filter('kalman')
            except ImportError:
                return {
                    'name': filter_name,
                    'error': 'filterpy not installed'
                }
        else:
            filter_obj = get_filter(filter_name)
    except Exception as e:
        return {
            'name': filter_name,
            'error': str(e)
        }

    velocities = []
    distances = []
    process_times = []
    errors = []

    start_time = time.time()

    try:
        for point in data:
            t0 = time.time()

            try:
                if point.lat is not None:
                    # GPS update
                    v, d = filter_obj.update_gps(
                        point.lat, point.lon,
                        point.gps_speed, point.gps_accuracy
                    )
                elif point.accel_magnitude is not None and point.accel_magnitude > 0:
                    # Accel update
                    v, d = filter_obj.update_accelerometer(point.accel_magnitude)
                else:
                    continue

                velocities.append(v)
                distances.append(d)
                process_times.append(time.time() - t0)

            except Exception as e:
                errors.append({
                    'timestamp': point.timestamp,
                    'error': str(e)
                })

    except Exception as e:
        return {
            'name': filter_name,
            'error': f'Fatal: {str(e)}'
        }

    elapsed = time.time() - start_time

    return {
        'name': filter_name,
        'velocities': velocities,
        'distances': distances,
        'process_times': process_times,
        'total_time': elapsed,
        'errors': errors,
        'n_updates': len(velocities),
        'avg_update_time_ms': (np.mean(process_times) * 1000) if process_times else 0,
        'final_distance': distances[-1] if distances else 0,
        'max_velocity': max(velocities) if velocities else 0
    }


def compute_statistics(results):
    """Compute comparison statistics between filter results."""
    if len(results) < 2:
        return {}

    # Use first filter as reference
    ref = results[0]
    ref_distances = np.array(ref['distances'])

    stats = {}

    for i, r in enumerate(results[1:], 1):
        if 'error' in r:
            continue

        comp_distances = np.array(r['distances'])

        # Align arrays
        min_len = min(len(ref_distances), len(comp_distances))

        if min_len > 0:
            ref_aligned = ref_distances[:min_len]
            comp_aligned = comp_distances[:min_len]

            mae = np.mean(np.abs(ref_aligned - comp_aligned))
            rmse = np.sqrt(np.mean((ref_aligned - comp_aligned)**2))
            max_error = np.max(np.abs(ref_aligned - comp_aligned))

            stats[f"{ref['name']} vs {r['name']}"] = {
                'mae': mae,
                'rmse': rmse,
                'max_error': max_error
            }

    return stats


def print_report(results, stats):
    """Print formatted comparison report."""
    print("\n" + "="*80)
    print("FILTER COMPARISON REPORT")
    print("="*80)

    # Individual filter metrics
    print("\n### INDIVIDUAL FILTER METRICS ###\n")

    for r in results:
        print(f"Filter: {r['name'].upper()}")
        print("-" * 40)

        if 'error' in r:
            print(f"ERROR: {r['error']}\n")
            continue

        print(f"  Updates processed:      {r['n_updates']}")
        print(f"  Total runtime:          {r['total_time']:.3f}s")
        print(f"  Avg update time:        {r['avg_update_time_ms']:.3f}ms")
        print(f"  Final distance:         {r['final_distance']:.2f}m")
        print(f"  Max velocity:           {r['max_velocity']:.2f}m/s ({r['max_velocity']*3.6:.1f}km/h)")
        print(f"  Errors during run:      {len(r['errors'])}")

        if r['errors']:
            print(f"  First error: {r['errors'][0]}\n")
        else:
            print()

    # Comparative analysis
    if stats:
        print("\n### COMPARATIVE ANALYSIS ###\n")

        for comparison, metrics in stats.items():
            print(f"{comparison}")
            print("-" * 40)
            print(f"  Mean Absolute Error:    {metrics['mae']:.3f}m")
            print(f"  Root Mean Square Error: {metrics['rmse']:.3f}m")
            print(f"  Maximum Error:          {metrics['max_error']:.3f}m\n")

    # Recommendations
    print("\n### FILTER RECOMMENDATIONS ###\n")
    print("Complementary:  Fast, simple, good for testing/baseline")
    print("Kalman-Numpy:   Pure numpy, no dependencies, faster than filterpy")
    print("Kalman:         Linear model, requires filterpy (use numpy version instead)")
    print("EKF:            Handles GPS non-linearity, recommended for production")
    print("UKF:            Most accurate but slower, for high-precision offline analysis\n")

    print("="*80)


def main():
    parser = argparse.ArgumentParser(description='Compare sensor fusion filters')
    parser.add_argument('--synthetic', action='store_true',
                       help='Use synthetic test data')
    parser.add_argument('--session-file', type=str,
                       help='Load motion tracker session JSON file')
    parser.add_argument('--duration', type=int, default=300,
                       help='Duration of synthetic data (seconds)')

    args = parser.parse_args()

    # Load data
    if args.session_file:
        print(f"Loading session from {args.session_file}...")
        data = load_session_data(args.session_file)
    else:
        print(f"Generating {args.duration}s of synthetic data...")
        data = generate_synthetic_data(args.duration)

    print(f"Loaded {len(data)} data points\n")

    # Run all filters
    filters_to_test = ['complementary', 'kalman-numpy', 'kalman', 'ekf', 'ukf']
    results = []

    for filter_name in filters_to_test:
        print(f"Testing {filter_name}...", end='', flush=True)
        result = run_filter_on_data(filter_name, data)
        results.append(result)
        if 'error' not in result:
            print(f" ✓ ({result['n_updates']} updates)")
        else:
            print(f" ✗ ({result['error']})")

    # Compute statistics
    stats = compute_statistics(results)

    # Print report
    print_report(results, stats)


if __name__ == '__main__':
    main()
