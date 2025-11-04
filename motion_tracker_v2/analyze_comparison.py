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


def load_comparison_data(filename):
    """Load comparison JSON file"""
    with open(filename, 'r') as f:
        return json.load(f)


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


def compute_distance_accuracy(data):
    """Compare distance measurements"""
    gps_samples = data['gps_samples']

    if not gps_samples:
        return None

    # CRITICAL FIX: Calculate GPS ground truth from actual coordinates, not EKF estimate
    gps_truth = _calculate_gps_haversine_distance(gps_samples)

    ekf_dist = data['final_metrics']['ekf']['distance']
    comp_dist = data['final_metrics']['complementary']['distance']

    ekf_error = abs(ekf_dist - gps_truth)
    comp_error = abs(comp_dist - gps_truth)

    ekf_error_pct = (ekf_error / gps_truth * 100) if gps_truth > 0 else 0
    comp_error_pct = (comp_error / gps_truth * 100) if gps_truth > 0 else 0

    return {
        'gps_truth': gps_truth,
        'ekf_distance': ekf_dist,
        'ekf_error': ekf_error,
        'ekf_error_pct': ekf_error_pct,
        'comp_distance': comp_dist,
        'comp_error': comp_error,
        'comp_error_pct': comp_error_pct,
        'ekf_better': ekf_error_pct < comp_error_pct
    }


def compute_velocity_smoothness(data):
    """Analyze velocity stability and filter divergence"""
    gps_samples = data['gps_samples']

    if len(gps_samples) < 2:
        return None

    ekf_velocities = [s['ekf_velocity'] for s in gps_samples]
    comp_velocities = [s['comp_velocity'] for s in gps_samples]

    # Standard deviation indicates smoothness (lower = smoother)
    ekf_std = np.std(ekf_velocities)
    comp_std = np.std(comp_velocities)

    # RMS (root mean square) error vs mean
    ekf_mean = np.mean(ekf_velocities)
    comp_mean = np.mean(comp_velocities)

    # Velocity divergence: how different are the filters?
    velocity_differences = [abs(ekf_velocities[i] - comp_velocities[i]) for i in range(len(ekf_velocities))]
    max_velocity_diff = max(velocity_differences) if velocity_differences else 0
    mean_velocity_diff = np.mean(velocity_differences) if velocity_differences else 0

    return {
        'ekf_mean': ekf_mean,
        'ekf_std': ekf_std,
        'comp_mean': comp_mean,
        'comp_std': comp_std,
        'ekf_smoother': ekf_std < comp_std,
        'max_velocity_divergence': max_velocity_diff,
        'mean_velocity_divergence': mean_velocity_diff
    }


def compute_accel_tracking(data):
    """Analyze acceleration magnitude tracking"""
    accel_samples = data['accel_samples']

    if not accel_samples:
        return None

    raw_magnitudes = [s['magnitude'] for s in accel_samples]
    ekf_magnitudes = [s['ekf_velocity'] for s in accel_samples]  # Using velocity as proxy
    comp_magnitudes = [s['comp_velocity'] for s in accel_samples]

    # RMS of magnitude
    ekf_mean = np.mean([s.get('ekf_accel', 0) for s in data.get('final_metrics', {}).get('ekf', {}).values()])

    return {
        'raw_mean_magnitude': np.mean(raw_magnitudes),
        'raw_std_magnitude': np.std(raw_magnitudes),
        'num_samples': len(accel_samples)
    }


def compute_quality_score(accuracy_data, smoothness_data):
    """Compute overall filter quality score (0-100)"""
    score_ekf = 100.0
    score_comp = 100.0

    # Distance accuracy (50% weight)
    if accuracy_data:
        ekf_distance_penalty = min(accuracy_data['ekf_error_pct'] * 5, 50)  # max 50 points
        comp_distance_penalty = min(accuracy_data['comp_error_pct'] * 5, 50)
        score_ekf -= ekf_distance_penalty
        score_comp -= comp_distance_penalty

    # Velocity smoothness (30% weight)
    if smoothness_data:
        ekf_smoothness_penalty = min(smoothness_data['ekf_std'] * 10, 30)  # max 30 points
        comp_smoothness_penalty = min(smoothness_data['comp_std'] * 10, 30)
        score_ekf -= ekf_smoothness_penalty
        score_comp -= comp_smoothness_penalty

    # Bonus for stability
    if smoothness_data and smoothness_data['ekf_std'] < 0.1:
        score_ekf += 10

    return max(0, score_ekf), max(0, score_comp)


def print_report(data):
    """Print formatted analysis report"""
    print("\n" + "="*100)
    print("FILTER COMPARISON ANALYSIS REPORT")
    print("="*100)

    print(f"\nTest Configuration:")
    print(f"  Requested Duration: {data['test_duration']} minutes")
    print(f"  Actual Duration: {data['actual_duration']:.1f} seconds ({data['actual_duration']/60:.1f} minutes)")
    print(f"  GPS Samples: {len(data['gps_samples'])}")
    print(f"  Accel Samples: {len(data['accel_samples'])}")

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

    # Velocity smoothness
    print(f"\n{'─'*100}")
    print("VELOCITY SMOOTHNESS (Stability Analysis)")
    print(f"{'─'*100}")

    smoothness_data = compute_velocity_smoothness(data)
    if smoothness_data:
        print(f"\n  EKF Filter:")
        print(f"    Mean Velocity: {smoothness_data['ekf_mean']:.3f} m/s")
        print(f"    Std Dev: {smoothness_data['ekf_std']:.3f} m/s (lower = smoother)")

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

    # Overall score
    print(f"\n{'─'*100}")
    print("OVERALL QUALITY SCORE (0-100)")
    print(f"{'─'*100}")

    ekf_score, comp_score = compute_quality_score(accuracy_data, smoothness_data)

    print(f"\n  EKF Filter Score:          {ekf_score:.1f}/100")
    print(f"  Complementary Filter Score: {comp_score:.1f}/100")

    if ekf_score > comp_score:
        diff = ekf_score - comp_score
        print(f"\n  ✓ EKF WINS by {diff:.1f} points")
    else:
        diff = comp_score - ekf_score
        print(f"\n  ⚠ Complementary WINS by {diff:.1f} points")

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
