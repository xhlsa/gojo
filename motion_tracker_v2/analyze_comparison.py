#!/usr/bin/env python3
"""
Analyze Filter Comparison Results

Reads the JSON output from test_ekf_vs_complementary.py and computes:
- Distance accuracy vs GPS
- Velocity smoothness
- Stationary detection performance
- Overall filter quality score

Usage:
    python analyze_comparison.py comparison_2025-10-28_14-30-45.json
"""

import sys
import json
import math
import numpy as np
from pathlib import Path
from copy import deepcopy


def load_comparison_data(filename):
    """Load comparison JSON file"""
    with open(filename, 'r') as f:
        return json.load(f)


def _extract_gps_samples(data):
    """Return GPS samples regardless of schema."""
    if 'gps_samples' in data:
        return data['gps_samples']

    samples = []
    for reading in data.get('readings', []):
        gps = reading.get('gps')
        if not gps:
            continue
        if 'latitude' not in gps or 'longitude' not in gps:
            continue
        samples.append({
            'timestamp': gps.get('timestamp', reading.get('timestamp', 0.0)),
            'latitude': gps['latitude'],
            'longitude': gps['longitude'],
            'accuracy': gps.get('accuracy', 0.0),
            'speed': gps.get('speed', 0.0),
            'provider': gps.get('provider', 'gps')
        })
    return samples


def _extract_accel_samples(data):
    """Return accelerometer samples with magnitudes regardless of schema."""
    if 'accel_samples' in data:
        return data['accel_samples']

    samples = []
    for reading in data.get('readings', []):
        accel = reading.get('accel')
        if not accel:
            continue
        ts = accel.get('timestamp', reading.get('timestamp', 0.0))
        x = accel.get('x')
        y = accel.get('y')
        z = accel.get('z')
        if x is None or y is None or z is None:
            continue
        magnitude = math.sqrt(x * x + y * y + z * z)
        samples.append({'timestamp': ts, 'magnitude': magnitude})
    return samples


def _calculate_gps_haversine_distance(gps_samples):
    """Calculate actual GPS distance from coordinates using haversine formula.

    CRITICAL FIX: Do NOT use EKF's distance estimate as ground truth.
    Calculate true GPS distance by accumulating haversine distances.
    """
    if len(gps_samples) < 2:
        return 0.0

    total_distance = 0.0
    for i in range(1, len(gps_samples)):
        prev = gps_samples[i-1]
        curr = gps_samples[i]

        lat1 = prev['latitude']
        lon1 = prev['longitude']
        lat2 = curr['latitude']
        lon2 = curr['longitude']

        # Haversine formula
        R = 6371000  # Earth radius in meters
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        delta_phi = math.radians(lat2 - lat1)
        delta_lambda = math.radians(lon2 - lon1)

        a = (math.sin(delta_phi/2) ** 2 +
             math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda/2) ** 2)
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

        distance_increment = R * c
        total_distance += distance_increment

    return total_distance


def _get_filter_metric(data, filter_name, field):
    """Retrieve filter metric regardless of schema."""
    final_metrics = data.get('final_metrics', {})
    if filter_name in final_metrics:
        return final_metrics[filter_name].get(field)

    # Rust schema: fallback to stats for EKF
    if filter_name == 'ekf':
        stats = data.get('stats', {})
        if field == 'distance':
            return stats.get('ekf_distance')
        if field == 'velocity':
            return stats.get('ekf_velocity')
    return None


def compute_distance_accuracy(data):
    """Compare distance measurements"""
    gps_samples = _extract_gps_samples(data)

    if not gps_samples:
        return None

    gps_truth = _calculate_gps_haversine_distance(gps_samples)

    ekf_dist = _get_filter_metric(data, 'ekf', 'distance')
    if ekf_dist is None:
        return None

    comp_dist = _get_filter_metric(data, 'complementary', 'distance')

    ekf_error = abs(ekf_dist - gps_truth)
    ekf_error_pct = (ekf_error / gps_truth * 100) if gps_truth > 0 else 0

    comp_error = None
    comp_error_pct = None
    if comp_dist is not None:
        comp_error = abs(comp_dist - gps_truth)
        comp_error_pct = (comp_error / gps_truth * 100) if gps_truth > 0 else 0

    return {
        'gps_truth': gps_truth,
        'ekf_distance': ekf_dist,
        'ekf_error': ekf_error,
        'ekf_error_pct': ekf_error_pct,
        'comp_distance': comp_dist,
        'comp_error': comp_error,
        'comp_error_pct': comp_error_pct,
        'ekf_better': comp_error_pct is not None and ekf_error_pct < comp_error_pct
    }


def _extract_velocity_series(data):
    """
    Return aligned EKF/complementary velocity lists even when schemas differ.
    """
    gps_samples = _extract_gps_samples(data)
    if not gps_samples:
        return None, None

    # Preferred path: gps_samples already include velocities
    if all('ekf_velocity' in sample and 'comp_velocity' in sample for sample in gps_samples):
        ekf_velocities = [sample['ekf_velocity'] for sample in gps_samples]
        comp_velocities = [sample['comp_velocity'] for sample in gps_samples]
        return ekf_velocities, comp_velocities

    trajectories = data.get('trajectories')

    # Rust schema stores both velocities in a single list
    if isinstance(trajectories, list) and trajectories:
        ekf_velocities = [t.get('ekf_velocity') for t in trajectories if t.get('ekf_velocity') is not None]
        comp_velocities = [t.get('comp_velocity') for t in trajectories if t.get('comp_velocity') is not None]
        if ekf_velocities and comp_velocities:
            aligned_len = min(len(ekf_velocities), len(comp_velocities))
            return ekf_velocities[:aligned_len], comp_velocities[:aligned_len]
        return None, None

    # Older schema: trajectories keyed by filter name
    if isinstance(trajectories, dict):
        ekf_track = trajectories.get('ekf') or []
        comp_track = trajectories.get('complementary') or []

        aligned_len = min(len(gps_samples), len(ekf_track), len(comp_track))
        if aligned_len < 2:
            return None, None

        ekf_velocities = [
            ekf_track[i].get('velocity', 0.0)
            for i in range(aligned_len)
        ]
        comp_velocities = [
            comp_track[i].get('velocity', 0.0)
            for i in range(aligned_len)
        ]
        return ekf_velocities, comp_velocities

    return None, None


def compute_velocity_smoothness(data):
    """Analyze velocity stability and filter divergence"""
    ekf_velocities, comp_velocities = _extract_velocity_series(data)
    if not ekf_velocities:
        return None

    ekf_std = np.std(ekf_velocities)
    ekf_mean = np.mean(ekf_velocities)

    comp_std = None
    comp_mean = None
    ekf_smoother = None
    max_velocity_diff = None
    mean_velocity_diff = None

    if comp_velocities:
        comp_std = np.std(comp_velocities)
        comp_mean = np.mean(comp_velocities)
        velocity_differences = [
            abs(ekf_velocities[i] - comp_velocities[i])
            for i in range(min(len(ekf_velocities), len(comp_velocities)))
        ]
        if velocity_differences:
            max_velocity_diff = max(velocity_differences)
            mean_velocity_diff = np.mean(velocity_differences)
        ekf_smoother = ekf_std < comp_std if comp_std is not None else None

    return {
        'ekf_mean': ekf_mean,
        'ekf_std': ekf_std,
        'comp_mean': comp_mean,
        'comp_std': comp_std,
        'ekf_smoother': ekf_smoother,
        'max_velocity_divergence': max_velocity_diff,
        'mean_velocity_divergence': mean_velocity_diff
    }


def compute_accel_tracking(data):
    """Analyze acceleration magnitude tracking"""
    accel_samples = _extract_accel_samples(data)

    if not accel_samples:
        return None

    raw_magnitudes = [s.get('magnitude', 0.0) for s in accel_samples]

    return {
        'raw_mean_magnitude': np.mean(raw_magnitudes),
        'raw_std_magnitude': np.std(raw_magnitudes),
        'num_samples': len(accel_samples)
    }


def compute_quality_score(accuracy_data, smoothness_data):
    """Compute overall filter quality score (0-100)"""
    score_ekf = 100.0
    score_comp = 100.0 if accuracy_data and accuracy_data.get('comp_error_pct') is not None else None

    # Distance accuracy (50% weight)
    if accuracy_data:
        ekf_distance_penalty = min(accuracy_data['ekf_error_pct'] * 5, 50)  # max 50 points
        score_ekf -= ekf_distance_penalty
        if score_comp is not None:
            comp_distance_penalty = min(accuracy_data['comp_error_pct'] * 5, 50)
            score_comp -= comp_distance_penalty

    # Velocity smoothness (30% weight)
    if smoothness_data:
        ekf_smoothness_penalty = min(smoothness_data['ekf_std'] * 10, 30)  # max 30 points
        score_ekf -= ekf_smoothness_penalty
        if score_comp is not None and smoothness_data.get('comp_std') is not None:
            comp_smoothness_penalty = min(smoothness_data['comp_std'] * 10, 30)
            score_comp -= comp_smoothness_penalty

    # Bonus for stability
    if smoothness_data and smoothness_data['ekf_std'] < 0.1:
        score_ekf += 10

    final_comp = max(0, score_comp) if score_comp is not None else None
    return max(0, score_ekf), final_comp


def print_report(data):
    """Print formatted analysis report"""
    print("\n" + "="*100)
    print("FILTER COMPARISON ANALYSIS REPORT")
    print("="*100)

    gps_samples = _extract_gps_samples(data)
    accel_samples = _extract_accel_samples(data)

    requested_minutes = data.get('test_duration')
    metrics = data.get('metrics', {})
    if requested_minutes is None and metrics.get('test_duration_seconds') is not None:
        requested_minutes = metrics['test_duration_seconds'] / 60.0

    actual_seconds = data.get('actual_duration')
    if actual_seconds is None and metrics.get('test_duration_seconds') is not None:
        actual_seconds = metrics['test_duration_seconds']

    print(f"\nTest Configuration:")
    if requested_minutes is not None:
        print(f"  Requested Duration: {requested_minutes:.1f} minutes")
    else:
        print("  Requested Duration: Unknown")

    if actual_seconds is not None:
        print(f"  Actual Duration: {actual_seconds:.1f} seconds ({actual_seconds/60:.1f} minutes)")
    else:
        print("  Actual Duration: Unknown")

    print(f"  GPS Samples: {len(gps_samples)}")
    print(f"  Accel Samples: {len(accel_samples)}")

    # Distance accuracy
    print(f"\n{'─'*100}")
    print("DISTANCE ACCURACY (vs GPS Ground Truth)")
    print(f"{'─'*100}")

    accuracy_data = compute_distance_accuracy(data)
    if accuracy_data:
        print(f"\n  GPS Truth Distance: {accuracy_data['gps_truth']:.2f} m")
        print(f"\n  EKF Filter:")
        print(f"    Reported Distance: {accuracy_data['ekf_distance']:.2f} m")
        print(f"    Error: {accuracy_data['ekf_error']:.2f} m ({accuracy_data['ekf_error_pct']:.2f}%)")

        if accuracy_data['comp_distance'] is not None and accuracy_data['comp_error_pct'] is not None:
            print(f"\n  Complementary Filter:")
            print(f"    Reported Distance: {accuracy_data['comp_distance']:.2f} m")
            print(f"    Error: {accuracy_data['comp_error']:.2f} m ({accuracy_data['comp_error_pct']:.2f}%)")

            if accuracy_data['ekf_better']:
                improvement = ((accuracy_data['comp_error_pct'] - accuracy_data['ekf_error_pct']) /
                             accuracy_data['comp_error_pct'] * 100)
                print(f"\n  ✓ EKF is {improvement:.1f}% MORE ACCURATE than Complementary")
            else:
                degradation = ((accuracy_data['ekf_error_pct'] - accuracy_data['comp_error_pct']) /
                             accuracy_data['comp_error_pct'] * 100)
                print(f"\n  ⚠ EKF is {degradation:.1f}% LESS accurate than Complementary")
        else:
            print("\n  Complementary metrics unavailable in this dataset.")

    # Velocity smoothness
    print(f"\n{'─'*100}")
    print("VELOCITY SMOOTHNESS (Stability Analysis)")
    print(f"{'─'*100}")

    smoothness_data = compute_velocity_smoothness(data)
    if smoothness_data:
        print(f"\n  EKF Filter:")
        print(f"    Mean Velocity: {smoothness_data['ekf_mean']:.3f} m/s")
        print(f"    Std Dev: {smoothness_data['ekf_std']:.3f} m/s (lower = smoother)")

        if smoothness_data['comp_mean'] is not None:
            print(f"\n  Complementary Filter:")
            print(f"    Mean Velocity: {smoothness_data['comp_mean']:.3f} m/s")
            print(f"    Std Dev: {smoothness_data['comp_std']:.3f} m/s")

            print(f"\n  Filter Divergence (How Different They Are):")
            print(f"    Mean Difference: {smoothness_data['mean_velocity_divergence']:.3f} m/s")
            print(f"    Max Difference:  {smoothness_data['max_velocity_divergence']:.3f} m/s")
            print(f"    → Higher divergence = filters responding differently to motion (good)")

            if smoothness_data['ekf_smoother']:
                smoothness_improvement = ((smoothness_data['comp_std'] - smoothness_data['ekf_std']) /
                                         smoothness_data['comp_std'] * 100)
                print(f"\n  ✓ EKF is {smoothness_improvement:.1f}% SMOOTHER (lower variance)")
            else:
                smoothness_degradation = ((smoothness_data['ekf_std'] - smoothness_data['comp_std']) /
                                         smoothness_data['comp_std'] * 100)
                print(f"\n  ⚠ EKF is {smoothness_degradation:.1f}% LESS smooth (higher variance)")
        else:
            print("\n  Complementary velocity data unavailable in this dataset.")

    # Overall score
    print(f"\n{'─'*100}")
    print("OVERALL QUALITY SCORE (0-100)")
    print(f"{'─'*100}")

    ekf_score, comp_score = compute_quality_score(accuracy_data, smoothness_data)

    print(f"\n  EKF Filter Score:          {ekf_score:.1f}/100")
    if comp_score is not None:
        print(f"  Complementary Filter Score: {comp_score:.1f}/100")
        if ekf_score > comp_score:
            diff = ekf_score - comp_score
            print(f"\n  ✓ EKF WINS by {diff:.1f} points")
        else:
            diff = comp_score - ekf_score
            print(f"\n  ⚠ Complementary WINS by {diff:.1f} points")
    else:
        print("  Complementary Filter Score: N/A (not available)")
        print("  ✓ EKF score computed from available data.")

    # Recommendations
    print(f"\n{'─'*100}")
    print("RECOMMENDATIONS")
    print(f"{'─'*100}")

    if ekf_score > 85:
        print("\n  ✓ EKF is PRODUCTION-READY")
        print("    - Excellent distance accuracy")
        print("    - Smooth velocity estimates")
        print("    - Safe to use as default filter")
    elif ekf_score > 75:
        print("\n  ◐ EKF is PROMISING")
        print("    - Good performance overall")
        print("    - Consider tuning noise parameters")
        print("    - Test with gyroscope enabled")
    else:
        print("\n  ⚠ EKF needs improvement")
        print("    - Review sensor calibration")
        print("    - Check GPS accuracy")
        print("    - Consider adjusting filter gains")

    print("\n" + "="*100)


def main():
    if len(sys.argv) < 2:
        print("Usage: python analyze_comparison.py <comparison_json_file>")
        sys.exit(1)

    filename = sys.argv[1]

    if not Path(filename).exists():
        print(f"ERROR: File not found: {filename}")
        sys.exit(1)

    print(f"Loading comparison data from: {filename}")
    data = load_comparison_data(filename)

    print_report(data)


if __name__ == '__main__':
    main()
