#!/usr/bin/env python3
"""
GOJO Race Engineer - AI-Assisted Post-Drive Telemetry Analysis

Analyzes enhanced EKF diagnostics to detect failure modes invisible to simple RMSE.
Generates compact AI-friendly summary for Claude Code review.

Usage:
    python3 scripts/race_engineer.py motion_tracker_sessions/comparison_20251204_183814.json.gz
"""

import json
import gzip
import sys
import numpy as np
import pandas as pd
from pathlib import Path
from typing import Dict, List, Tuple


def load_enhanced_log(filepath: Path) -> pd.DataFrame:
    """Load comparison log with enhanced diagnostics."""
    print(f"📦 Loading {filepath.name}...")

    with gzip.open(filepath, 'rt') as f:
        data = json.load(f)

    readings = data.get('readings', [])

    # Extract only GPS readings (where diagnostics exist)
    gps_readings = []
    for r in readings:
        if r.get('ekf_diagnostics'):
            diag = r['ekf_diagnostics']
            ekf = r.get('experimental_15d', {})
            vel = ekf.get('velocity', [0, 0, 0])

            gps_readings.append({
                'timestamp': r['timestamp'],
                'innovation': diag['innovation_magnitude'],
                'prediction_error': diag.get('prediction_error', 0.0),  # Backward compatible: default 0.0 if missing
                'nis': diag['nis'],
                'gps_rejected': diag['gps_rejected'],
                'snapped': diag['snapped'],
                'zupt_active': diag['zupt_active'],
                'p_pos_x': diag['p_pos_x'],
                'p_pos_y': diag['p_pos_y'],
                'p_pos_z': diag['p_pos_z'],
                'p_vel_x': diag['p_vel_x'],
                'p_vel_y': diag['p_vel_y'],
                'p_vel_z': diag['p_vel_z'],
                'linear_accel': diag['linear_accel_norm'],
                'turn_rate': diag['turn_rate'],
                'speed': np.linalg.norm(vel),
            })

    if not gps_readings:
        print("❌ No enhanced diagnostics found in log!")
        print("   Make sure you're using a log file generated after the diagnostics update.")
        sys.exit(1)

    df = pd.DataFrame(gps_readings)
    print(f"✓ Loaded {len(df)} GPS updates with diagnostics")
    return df


def segment_by_mode(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    """Segment drive into Brick vs Retriever vs Highway modes."""
    brick = df[df['speed'] < 3.0]        # Parking lot, stop-and-go
    retriever = df[(df['speed'] >= 3.0) & (df['speed'] < 15.0)]  # City driving
    highway = df[df['speed'] >= 15.0]    # Highway

    return {
        'brick': brick,
        'retriever': retriever,
        'highway': highway
    }


def compute_innovation_autocorr(df: pd.DataFrame, lag: int = 1) -> float:
    """
    Test A: Innovation Whiteness (Lag Detection)

    If innovations are correlated at lag-1, the filter has unmodeled dynamics
    (timestamp misalignment, wrong process noise, etc.)

    Target: < 0.2 (white noise)
    Warning: > 0.3 (systematic lag)
    """
    if len(df) < lag + 10:
        return np.nan

    return df['innovation'].autocorr(lag=lag)


def compute_turn_correlation(df: pd.DataFrame) -> float:
    """
    Test B: Turn-Induced Bias Detection

    Correlate NIS (accuracy) with turn rate. High correlation means
    filter accuracy degrades during cornering (gyro bias, centripetal, etc.)

    Target: < 0.2 (no turn dependence)
    Warning: > 0.4 (turn-induced errors)
    """
    if len(df) < 10:
        return np.nan

    # Filter out straight segments (turn_rate < 0.05 rad/s)
    turning = df[df['turn_rate'].abs() > 0.05]

    if len(turning) < 10:
        return np.nan

    return turning['nis'].corr(turning['turn_rate'].abs())


def compute_accel_correlation(df: pd.DataFrame) -> float:
    """
    Test C: Hard-Braking/Acceleration Bias

    Does accuracy degrade during hard accel/decel?
    High correlation suggests accel bias estimation is wrong.

    Target: < 0.2
    Warning: > 0.4
    """
    if len(df) < 10:
        return np.nan

    # Filter high dynamics (linear_accel > 2.0 m/s²)
    high_g = df[df['linear_accel'] > 2.0]

    if len(high_g) < 10:
        return np.nan

    return high_g['nis'].corr(high_g['linear_accel'])


def analyze_stop_drift(df: pd.DataFrame) -> List[Dict]:
    """
    Test D: Stop-Light Drift Rate

    Isolate stationary segments (ZUPT active) and measure position drift.
    Good filters should hold position to < 0.5m over 60s when stopped.
    """
    stops = df[df['zupt_active'] == True].copy()

    if len(stops) < 10:
        return []

    # Identify continuous stop segments (gaps < 5s)
    stops['time_gap'] = stops['timestamp'].diff()
    stop_segments = []

    current_segment = []
    for idx, row in stops.iterrows():
        if len(current_segment) == 0 or row['time_gap'] < 5.0:
            current_segment.append(row)
        else:
            if len(current_segment) >= 10:  # At least 10 samples (10s)
                stop_segments.append(current_segment)
            current_segment = [row]

    # Add last segment
    if len(current_segment) >= 10:
        stop_segments.append(current_segment)

    # Compute drift for each segment
    drift_analysis = []
    for seg in stop_segments:
        duration = seg[-1]['timestamp'] - seg[0]['timestamp']

        # Drift = sqrt(variance of position uncertainty)
        # Approximation: use covariance diagonal growth
        p_start = seg[0]['p_pos_x'] + seg[0]['p_pos_y']
        p_end = seg[-1]['p_pos_x'] + seg[-1]['p_pos_y']
        drift_uncertainty = np.sqrt(p_end) - np.sqrt(p_start)

        drift_analysis.append({
            'start_time': seg[0]['timestamp'],
            'duration': duration,
            'drift_uncertainty_growth': drift_uncertainty,
        })

    return drift_analysis


def analyze_snaps(df: pd.DataFrame) -> List[Dict]:
    """
    Test E: Divergence Snap Analysis

    When/why did the filter snap to GPS (dist > 30m)?
    These are catastrophic failures that need investigation.
    """
    snaps = df[df['snapped'] == True]

    if len(snaps) == 0:
        return []

    snap_events = []
    for idx, snap in snaps.iterrows():
        # Find context: what was happening before the snap?
        context_window = df[(df['timestamp'] >= snap['timestamp'] - 5.0) &
                            (df['timestamp'] < snap['timestamp'])]

        avg_speed_before = context_window['speed'].mean() if len(context_window) > 0 else 0
        avg_nis_before = context_window['nis'].mean() if len(context_window) > 0 else 0

        snap_events.append({
            'timestamp': snap['timestamp'],
            'innovation': snap['innovation'],
            'speed': snap['speed'],
            'avg_speed_5s_before': avg_speed_before,
            'avg_nis_5s_before': avg_nis_before,
        })

    return snap_events


def analyze_prediction_bias(df: pd.DataFrame) -> Dict:
    """
    Test F: Prediction Model Bias Detection

    Analyzes prediction errors to detect systematic biases in motion model.
    Prediction error = distance from EKF prediction to GPS measurement [meters].

    Returns dict with bias metrics:
      - mean_error: Average prediction error across all updates
      - median_error: Median (robust to outliers)
      - p95_error: 95th percentile (tail behavior)
      - bias_by_speed: Dict of error stats segmented by speed regime
      - high_error_fraction: Fraction of updates > 5m prediction error
    """
    if len(df) < 10:
        return {}

    pred_errors = df['prediction_error']

    metrics = {
        'mean_error': pred_errors.mean(),
        'median_error': pred_errors.median(),
        'p95_error': pred_errors.quantile(0.95),
        'std_dev': pred_errors.std(),
        'high_error_fraction': (pred_errors > 5.0).sum() / len(df),
    }

    # Bias by speed regime
    brick_speed = df[df['speed'] < 3.0]['prediction_error']
    retriever_speed = df[(df['speed'] >= 3.0) & (df['speed'] < 15.0)]['prediction_error']
    highway_speed = df[df['speed'] >= 15.0]['prediction_error']

    metrics['bias_by_speed'] = {
        'brick': brick_speed.mean() if len(brick_speed) > 0 else np.nan,
        'retriever': retriever_speed.mean() if len(retriever_speed) > 0 else np.nan,
        'highway': highway_speed.mean() if len(highway_speed) > 0 else np.nan,
    }

    return metrics


def generate_race_engineer_report(filepath: Path):
    """Main analysis pipeline - generates AI-friendly diagnostic summary."""

    df = load_enhanced_log(filepath)

    # Segment by driving mode
    segments = segment_by_mode(df)

    print("\n" + "="*60)
    print("           GOJO RACE ENGINEER REPORT")
    print("="*60)

    # === BASIC STATS ===
    duration = df['timestamp'].iloc[-1] - df['timestamp'].iloc[0]
    print(f"\n📊 SESSION OVERVIEW")
    print(f"   Duration: {duration:.1f}s ({duration/60:.1f} min)")
    print(f"   GPS Updates: {len(df)}")
    print(f"   GPS Rejection Rate: {df['gps_rejected'].sum() / len(df) * 100:.1f}%")

    # Mode split
    print(f"\n🚗 DRIVING MODE SPLIT")
    for mode, seg_df in segments.items():
        pct = len(seg_df) / len(df) * 100
        avg_nis = seg_df['nis'].median() if len(seg_df) > 0 else np.nan
        print(f"   {mode.upper():10s}: {pct:5.1f}%  |  Median NIS: {avg_nis:.2f}")

    # === ADVANCED DIAGNOSTICS ===
    print(f"\n🔬 ADVANCED DIAGNOSTICS")

    # Test A: Innovation Autocorrelation
    autocorr = compute_innovation_autocorr(df, lag=1)
    autocorr_verdict = "✅ GOOD" if autocorr < 0.2 else ("⚠️  WARNING" if autocorr < 0.4 else "❌ CRITICAL")
    print(f"   Innovation Autocorr (lag-1): {autocorr:.3f}  {autocorr_verdict}")
    if autocorr > 0.3:
        print(f"      → Likely cause: GPS timestamp lag or wrong process noise Q")

    # Test B: Turn Correlation
    turn_corr = compute_turn_correlation(df)
    turn_verdict = "✅ GOOD" if abs(turn_corr) < 0.2 else ("⚠️  WARNING" if abs(turn_corr) < 0.4 else "❌ CRITICAL")
    print(f"   Turn Correlation (NIS vs |gyro_z|): {turn_corr:.3f}  {turn_verdict}")
    if abs(turn_corr) > 0.3:
        print(f"      → Likely cause: Gyro bias estimation or centripetal compensation")

    # Test C: Accel Correlation
    accel_corr = compute_accel_correlation(df)
    accel_verdict = "✅ GOOD" if abs(accel_corr) < 0.2 else ("⚠️  WARNING" if abs(accel_corr) < 0.4 else "❌ CRITICAL")
    print(f"   Accel Correlation (NIS vs |a|): {accel_corr:.3f}  {accel_verdict}")
    if abs(accel_corr) > 0.3:
        print(f"      → Likely cause: Accel bias not tracking true sensor drift")

    # Test D: Stop Drift
    print(f"\n🛑 STOP-LIGHT DRIFT ANALYSIS")
    stop_segments = analyze_stop_drift(df)
    if stop_segments:
        print(f"   Detected {len(stop_segments)} stationary segments:")
        for i, seg in enumerate(stop_segments[:5], 1):  # Show first 5
            print(f"      Stop #{i} @{seg['start_time']:.1f}s: {seg['duration']:.0f}s, "
                  f"Δσ_pos = {seg['drift_uncertainty_growth']:.2f}m")
    else:
        print(f"   No stationary segments detected (continuous motion)")

    # Test E: Snap Events
    print(f"\n💥 DIVERGENCE SNAP EVENTS")
    snap_events = analyze_snaps(df)
    if snap_events:
        print(f"   ❌ CRITICAL: {len(snap_events)} snaps triggered (filter diverged > 30m)")
        for i, snap in enumerate(snap_events, 1):
            print(f"      Snap #{i} @{snap['timestamp']:.1f}s: innovation={snap['innovation']:.1f}m, "
                  f"speed={snap['speed']:.1f} m/s")
            print(f"         Context (5s before): avg_speed={snap['avg_speed_5s_before']:.1f} m/s, "
                  f"avg_NIS={snap['avg_nis_5s_before']:.1f}")
    else:
        print(f"   ✅ No snaps triggered (filter remained converged)")

    # Test F: Prediction Bias
    print(f"\n🎯 PREDICTION BIAS ANALYSIS")
    pred_bias = analyze_prediction_bias(df)
    if pred_bias:
        mean_pred = pred_bias['mean_error']
        median_pred = pred_bias['median_error']
        p95_pred = pred_bias['p95_error']
        high_error_frac = pred_bias['high_error_fraction']

        bias_verdict = "✅ GOOD" if mean_pred < 2.0 else ("⚠️  WARNING" if mean_pred < 5.0 else "❌ CRITICAL")
        print(f"   Mean Prediction Error: {mean_pred:.2f}m  {bias_verdict}")
        print(f"   Median: {median_pred:.2f}m  |  95th%ile: {p95_pred:.2f}m  |  High-error fraction: {high_error_frac:.1%}")

        # Speed regime breakdown
        bias_speed = pred_bias['bias_by_speed']
        print(f"   Bias by Speed Regime:")
        print(f"      Brick (<3 m/s):    {bias_speed['brick']:.2f}m" if not np.isnan(bias_speed['brick']) else "      Brick (<3 m/s):    N/A")
        print(f"      Retriever (3-15):  {bias_speed['retriever']:.2f}m" if not np.isnan(bias_speed['retriever']) else "      Retriever (3-15):  N/A")
        print(f"      Highway (>15):     {bias_speed['highway']:.2f}m" if not np.isnan(bias_speed['highway']) else "      Highway (>15):     N/A")

        if mean_pred > 5.0:
            print(f"      → Motion model has significant bias. Check accel/gyro calibration.")
        elif mean_pred > 2.0:
            print(f"      → Moderate bias detected. May need tighter process noise (Q) tuning.")
    else:
        print(f"   Insufficient prediction data")

    # === NIS DISTRIBUTION ===
    print(f"\n📈 NIS DISTRIBUTION (Target: 2.0-4.0)")
    nis_median = df['nis'].median()
    nis_p95 = df['nis'].quantile(0.95)
    nis_verdict = ("✅ GOOD" if 2.0 <= nis_median <= 4.0 else
                   ("⚠️  OVERCONFIDENT" if nis_median > 10.0 else "⚠️  UNDERCONFIDENT"))
    print(f"   Median: {nis_median:.2f}  |  95th percentile: {nis_p95:.2f}  {nis_verdict}")

    if nis_median > 10.0:
        print(f"      → Filter too confident in IMU. Increase Q (process noise)")
    elif nis_median < 0.5:
        print(f"      → Filter too uncertain. Decrease Q or check GPS noise R")

    # === COVARIANCE HEALTH ===
    print(f"\n🧬 COVARIANCE HEALTH")
    p_pos_max = df[['p_pos_x', 'p_pos_y', 'p_pos_z']].max().max()
    p_vel_max = df[['p_vel_x', 'p_vel_y', 'p_vel_z']].max().max()
    print(f"   Max Position Uncertainty: {np.sqrt(p_pos_max):.2f} m")
    print(f"   Max Velocity Uncertainty: {np.sqrt(p_vel_max):.2f} m/s")

    if p_pos_max > 100.0:
        print(f"      ⚠️  WARNING: Position covariance grew large (GPS gaps?)")

    print("\n" + "="*60)
    print("✅ Analysis complete. Paste this report to Claude Code for review.")
    print("="*60 + "\n")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description='Race Engineer: AI-assisted post-drive telemetry analysis',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Analyze latest drive
  python3 scripts/race_engineer.py motion_tracker_sessions/comparison_20251204_183814.json.gz

  # Batch analyze all golden drives
  for f in motion_tracker_sessions/golden/*.json.gz; do
      python3 scripts/race_engineer.py "$f"
  done
        """
    )
    parser.add_argument('logfile', help='Path to comparison_*.json.gz file')

    args = parser.parse_args()

    logfile = Path(args.logfile)
    if not logfile.exists():
        print(f"❌ File not found: {logfile}")
        sys.exit(1)

    generate_race_engineer_report(logfile)
