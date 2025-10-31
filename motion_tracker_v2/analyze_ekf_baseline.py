#!/usr/bin/env python3
"""
Auto-analyze EKF vs Complementary Filter comparison results.

Run after each drive to see cumulative performance trends:
    python3 analyze_ekf_baseline.py

Or analyze specific file:
    python3 analyze_ekf_baseline.py comparison_20251031_120000.json.gz
"""

import json
import gzip
import math
from pathlib import Path
from datetime import datetime
from statistics import mean, stdev

def haversine_distance(lat1, lon1, lat2, lon2):
    """Calculate distance between two lat/lon points in meters"""
    R = 6371000  # Earth radius in meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (math.sin(delta_phi/2) ** 2 +
         math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda/2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

    return R * c

def load_comparison_file(filepath):
    """Load and parse comparison JSON file"""
    if str(filepath).endswith('.gz'):
        with gzip.open(filepath, 'rt') as f:
            return json.load(f)
    else:
        with open(filepath) as f:
            return json.load(f)

def calculate_gps_distance(gps_samples):
    """Calculate actual GPS distance using haversine"""
    if len(gps_samples) < 2:
        return 0.0

    total = 0.0
    for i in range(1, len(gps_samples)):
        prev = gps_samples[i-1]
        curr = gps_samples[i]
        dist = haversine_distance(
            prev['latitude'], prev['longitude'],
            curr['latitude'], curr['longitude']
        )
        total += dist

    return total

def analyze_single_file(filepath):
    """Analyze a single comparison file"""
    try:
        data = load_comparison_file(filepath)

        # Get distances
        gps_samples = data.get('gps_samples', [])
        ekf_dist = data['final_metrics']['ekf'].get('distance', 0)
        comp_dist = data['final_metrics']['complementary'].get('distance', 0)
        gps_dist = calculate_gps_distance(gps_samples)

        # Calculate errors
        ekf_error = abs(ekf_dist - gps_dist) / max(gps_dist, 0.001) * 100 if gps_dist > 0 else 0
        comp_error = abs(comp_dist - gps_dist) / max(gps_dist, 0.001) * 100 if gps_dist > 0 else 0
        advantage = comp_error - ekf_error

        # Get other metrics
        duration = data.get('actual_duration', 0)
        peak_memory = data.get('peak_memory_mb', 0)
        gps_fixes = len(gps_samples)

        # Extract timestamp from filename
        filename = Path(filepath).name
        try:
            # Format: comparison_YYYYMMDD_HHMMSS.json.gz
            parts = filename.replace('comparison_', '').replace('.json.gz', '').replace('.json', '')
            timestamp = parts
        except:
            timestamp = 'unknown'

        return {
            'file': filename,
            'timestamp': timestamp,
            'gps_distance': gps_dist,
            'ekf_distance': ekf_dist,
            'comp_distance': comp_dist,
            'ekf_error_pct': ekf_error,
            'comp_error_pct': comp_error,
            'ekf_advantage_pct': advantage,
            'duration_sec': duration,
            'peak_memory_mb': peak_memory,
            'gps_fixes': gps_fixes,
            'success': True
        }
    except Exception as e:
        return {
            'file': str(filepath),
            'error': str(e),
            'success': False
        }

def print_comparison_table(results):
    """Print formatted comparison table"""
    print("\n" + "="*120)
    print("EKF vs COMPLEMENTARY FILTER - PERFORMANCE COMPARISON")
    print("="*120)
    print(f"{'Date/Time':<20} {'GPS Dist':<12} {'EKF Err %':<12} {'Comp Err %':<12} {'EKF Adv %':<12} {'Dur (m)':<8} {'Mem (MB)':<10}")
    print("-"*120)

    for r in sorted(results, key=lambda x: x.get('timestamp', '')):
        if r.get('success'):
            timestamp = r['timestamp'][:12] if r.get('timestamp') else 'unknown'
            gps_dist = r['gps_distance']
            ekf_err = r['ekf_error_pct']
            comp_err = r['comp_error_pct']
            advantage = r['ekf_advantage_pct']
            duration = r['duration_sec'] / 60
            memory = r['peak_memory_mb']

            print(f"{timestamp:<20} {gps_dist:>10.0f}m  {ekf_err:>10.1f}%  {comp_err:>10.1f}%  {advantage:>10.1f}%  {duration:>6.1f}m  {memory:>8.1f}MB")

def print_summary_statistics(results):
    """Print summary statistics across all runs"""
    successful = [r for r in results if r.get('success')]

    if not successful:
        print("No successful analyses found.")
        return

    ekf_errors = [r['ekf_error_pct'] for r in successful]
    comp_errors = [r['comp_error_pct'] for r in successful]
    advantages = [r['ekf_advantage_pct'] for r in successful]
    durations = [r['duration_sec'] / 60 for r in successful]
    memories = [r['peak_memory_mb'] for r in successful]

    print("\n" + "="*120)
    print("SUMMARY STATISTICS")
    print("="*120)
    print(f"\nTests analyzed:        {len(successful)}")

    if len(successful) > 0:
        print(f"\nEKF Error:")
        print(f"  Mean:                {mean(ekf_errors):>6.2f}%")
        print(f"  Std Dev:             {stdev(ekf_errors) if len(ekf_errors) > 1 else 0:>6.2f}%")
        print(f"  Range:               {min(ekf_errors):>6.2f}% - {max(ekf_errors):>6.2f}%")

        print(f"\nComplementary Error:")
        print(f"  Mean:                {mean(comp_errors):>6.2f}%")
        print(f"  Std Dev:             {stdev(comp_errors) if len(comp_errors) > 1 else 0:>6.2f}%")
        print(f"  Range:               {min(comp_errors):>6.2f}% - {max(comp_errors):>6.2f}%")

        print(f"\nEKF Advantage:")
        print(f"  Mean:                {mean(advantages):>6.2f}% better")
        print(f"  Std Dev:             {stdev(advantages) if len(advantages) > 1 else 0:>6.2f}%")
        print(f"  Range:               {min(advantages):>6.2f}% - {max(advantages):>6.2f}%")

        # Consistency assessment
        advantage_range = max(advantages) - min(advantages)
        if advantage_range < 2:
            consistency = "VERY CONSISTENT (±<1%)"
        elif advantage_range < 5:
            consistency = "CONSISTENT (±2-3%)"
        elif advantage_range < 10:
            consistency = "VARIABLE (±5%)"
        else:
            consistency = "HIGHLY VARIABLE (±>5%)"

        print(f"\nConsistency:           {consistency}")

        print(f"\nMemory Usage:")
        print(f"  Mean:                {mean(memories):>6.1f} MB")
        print(f"  Range:               {min(memories):>6.1f} - {max(memories):>6.1f} MB")

        print(f"\nTest Duration:")
        print(f"  Mean:                {mean(durations):>6.1f} minutes")
        print(f"  Range:               {min(durations):>6.1f} - {max(durations):>6.1f} minutes")

def print_win_record(results):
    """Print which filter performed better in each test"""
    successful = [r for r in results if r.get('success')]

    ekf_wins = sum(1 for r in successful if r['ekf_error_pct'] < r['comp_error_pct'])
    comp_wins = sum(1 for r in successful if r['comp_error_pct'] < r['ekf_error_pct'])
    ties = sum(1 for r in successful if abs(r['ekf_error_pct'] - r['comp_error_pct']) < 0.1)

    print("\n" + "="*120)
    print("WIN RECORD")
    print("="*120)
    print(f"EKF wins:              {ekf_wins} / {len(successful)}")
    print(f"Complementary wins:    {comp_wins} / {len(successful)}")
    print(f"Ties:                  {ties} / {len(successful)}")

    if len(successful) > 0:
        ekf_win_pct = ekf_wins / len(successful) * 100
        print(f"\nEKF Win Rate:          {ekf_win_pct:.1f}%")

        if ekf_win_pct >= 80:
            verdict = "✅ EKF CLEARLY SUPERIOR - Justify increased complexity"
        elif ekf_win_pct >= 60:
            verdict = "✅ EKF GENERALLY BETTER - Worth the complexity"
        elif ekf_win_pct >= 50:
            verdict = "⚠️  EKF SLIGHTLY BETTER - Consider simpler filter"
        else:
            verdict = "❌ COMPLEMENTARY BETTER - Reconsider design choice"

        print(f"Verdict:               {verdict}")

def main():
    import sys

    # Get files to analyze
    if len(sys.argv) > 1:
        # Analyze specific file
        files = [Path(sys.argv[1])]
    else:
        # Find all comparison files
        sessions_dir = Path('/data/data/com.termux/files/home/gojo/motion_tracker_sessions')
        files = sorted(sessions_dir.glob('comparison_*.json*'))

    if not files:
        print("❌ No comparison files found in motion_tracker_sessions/")
        print("   Run: ./test_ekf.sh 5 --gyro")
        return

    # Analyze all files
    results = []
    for filepath in files:
        print(f"Analyzing {filepath.name}...", end=' ')
        result = analyze_single_file(filepath)
        results.append(result)
        print("✓" if result.get('success') else f"✗ ({result.get('error', 'unknown')})")

    # Print results
    print_comparison_table(results)
    print_summary_statistics(results)
    print_win_record(results)

    print("\n" + "="*120)
    print(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*120 + "\n")

if __name__ == '__main__':
    main()
