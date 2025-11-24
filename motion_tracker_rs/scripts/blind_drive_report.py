#!/usr/bin/env python3
"""
Blind Drive Report Card - Adapted for Gojo V2 Rust Output

Tests:
1. Velocity Stability (No 145M m/s explosions)
2. ZUPT Activation (Zero-velocity clamping)
3. Filter Confidence (Covariance stability)
4. Low-Speed Physics (Power floor validation)
"""

import json
import gzip
import numpy as np
import sys
from pathlib import Path

# CONFIG
ZUPT_TOLERANCE = 1e-9


def analyze_headless(file_path):
    print(f"Loading {file_path}...")

    # Handle both .gz and non-gz files
    if file_path.endswith('.gz'):
        with gzip.open(file_path, 'rt') as f:
            data = json.load(f)
    else:
        with open(file_path, 'r') as f:
            data = json.load(f)

    # Extract readings array
    readings = data.get('readings', data)  # Handle both formats

    print(f"Analyzing {len(readings):,} frames...")

    # Data extraction
    ekf_speeds = []
    cov_traces = []
    powers = []
    timestamps = []

    # Validation Counters
    zupt_active_count = 0
    low_speed_power_samples = 0
    velocity_samples = 0

    for entry in readings:
        # 1. Velocity & ZUPT
        ekf_15d = entry.get('experimental_15d')
        if not ekf_15d:
            continue

        vel = ekf_15d.get('velocity', [0, 0, 0])

        if vel and len(vel) == 3:
            v_mag = np.linalg.norm(vel)
            ekf_speeds.append(v_mag)
            velocity_samples += 1

            if v_mag < ZUPT_TOLERANCE:
                zupt_active_count += 1

            # 2. Covariance Trace
            cov_trace = ekf_15d.get('covariance_trace', 0.0)
            cov_traces.append(cov_trace)

            # 3. Power
            p = entry.get('specific_power_w_per_kg', 0.0)
            powers.append(p)

            # Check for Low Speed Power (0.1 < v < 2.0)
            if 0.1 < v_mag < 2.0 and p != 0.0:
                low_speed_power_samples += 1

    if not ekf_speeds:
        print("ERROR: No EKF velocity data found!")
        sys.exit(1)

    # Convert to numpy for stats
    ekf_speeds = np.array(ekf_speeds)
    cov_traces = np.array(cov_traces)
    powers = np.array(powers)

    # --- REPORT CARD ---
    max_speed = np.max(ekf_speeds)
    mean_speed = np.mean(ekf_speeds)
    max_cov = np.max(cov_traces)
    mean_cov = np.mean(cov_traces)

    print("\n" + "="*50)
    print("       GOJO V2 BLIND DRIVE REPORT CARD       ")
    print("="*50)

    # TEST 1: VELOCITY EXPLOSION (Did we fix the 145,000,000 m/s?)
    print(f"\n[1] VELOCITY STABILITY Check:")
    print(f"    Samples with velocity: {velocity_samples:,}")
    print(f"    Max Speed: {max_speed:.2f} m/s ({max_speed*2.237:.2f} mph / {max_speed*3.6:.2f} km/h)")
    print(f"    Mean Speed: {mean_speed:.2f} m/s ({mean_speed*2.237:.2f} mph / {mean_speed*3.6:.2f} km/h)")

    if max_speed < 60.0:  # ~134 mph buffer
        print("    STATUS: PASS ✅ (No explosions detected)")
    else:
        print(f"    STATUS: FAIL ❌ (Velocity exceeded 60 m/s)")

    # TEST 2: ZUPT ACTIVATION (Did we clamp to 0?)
    print(f"\n[2] ZUPT (Zero-Velocity Update) Check:")
    print(f"    Samples at exact 0.0 m/s: {zupt_active_count:,}")
    zupt_pct = 100 * zupt_active_count / len(ekf_speeds) if ekf_speeds.size > 0 else 0
    print(f"    Percentage: {zupt_pct:.2f}%")

    if zupt_active_count > 10:
        print("    STATUS: PASS ✅ (ZUPT logic is firing)")
    elif zupt_active_count > 0:
        print("    STATUS: PARTIAL ⚠️ (Some clamping detected)")
    else:
        print("    STATUS: WARNING ⚠️ (Never fully clamped to 0.0 - continuous motion)")

    # TEST 3: COVARIANCE HEALTH (Did P matrix explode?)
    print(f"\n[3] FILTER CONFIDENCE (Covariance) Check:")
    print(f"    Peak Covariance Trace: {max_cov:.2e}")
    print(f"    Mean Covariance Trace: {mean_cov:.2e}")

    if max_cov < 1000.0:
        print("    STATUS: PASS ✅ (Matrix remains tight)")
    elif max_cov < 10000.0:
        print("    STATUS: WARNING ⚠️ (Matrix getting large)")
    else:
        print(f"    STATUS: FAIL ❌ (Matrix blew up to {max_cov:.2e})")

    # TEST 4: PHYSICS AT LOW SPEED (Did we fix the power bug?)
    low_speed_total = np.sum((ekf_speeds > 0.1) & (ekf_speeds < 2.0))
    print(f"\n[4] LOW SPEED PHYSICS (Power Floor) Check:")
    print(f"    Low-speed samples (0.1-2.0 m/s): {low_speed_total:,}")
    print(f"    Valid power at low speed: {low_speed_power_samples:,}")

    if low_speed_total > 0:
        power_pct = 100 * low_speed_power_samples / low_speed_total
        print(f"    Coverage: {power_pct:.1f}%")

        if low_speed_power_samples > 0:
            print("    STATUS: PASS ✅ (Power metrics valid at low speed)")
        else:
            print("    STATUS: FAIL ❌ (Still zeroing out power at low speed)")
    else:
        print("    STATUS: N/A (No low-speed segments found)")

    # BONUS: Distance check
    if velocity_samples > 1:
        timestamps_arr = []
        for entry in readings:
            if entry.get('experimental_15d', {}).get('velocity'):
                timestamps_arr.append(entry['timestamp'])

        if len(timestamps_arr) > 1:
            timestamps_arr = np.array(timestamps_arr)
            dt = np.diff(timestamps_arr)
            valid_dt = dt[(dt > 0) & (dt < 1.0)]  # Sanity check

            if len(valid_dt) > 0:
                distances = ekf_speeds[1:len(valid_dt)+1] * valid_dt
                total_distance = np.sum(distances)

                print(f"\n[BONUS] INTEGRATED DISTANCE Check:")
                print(f"    EKF Distance: {total_distance:.1f} m ({total_distance/1000:.2f} km)")

    print("="*50)

    # Optional: Save a plot blindly
    try:
        import matplotlib
        matplotlib.use('Agg')  # Force headless backend
        import matplotlib.pyplot as plt

        fig, axes = plt.subplots(2, 1, figsize=(12, 8))

        # Plot 1: Speed
        axes[0].plot(ekf_speeds, 'b-', linewidth=0.5, alpha=0.7)
        axes[0].set_ylabel("Speed (m/s)")
        axes[0].set_title("EKF Velocity Profile")
        axes[0].grid(True, alpha=0.3)
        axes[0].axhline(y=60, color='r', linestyle='--', label='Explosion threshold')
        axes[0].legend()

        # Plot 2: Covariance
        axes[1].plot(cov_traces, 'r-', linewidth=0.5, alpha=0.7)
        axes[1].set_ylabel("Covariance Trace")
        axes[1].set_xlabel("Sample")
        axes[1].set_title("Filter Confidence (Lower = Better)")
        axes[1].grid(True, alpha=0.3)
        axes[1].set_yscale('log')

        plt.tight_layout()
        output_path = Path(file_path).parent / "velocity_check.png"
        plt.savefig(output_path, dpi=150)
        print(f"\n✓ Generated '{output_path.name}'")
        print(f"  View with: termux-open {output_path}")
    except ImportError:
        print("\n(matplotlib not available - skipping plots)")
    except Exception as e:
        print(f"\n(Plot generation failed: {e})")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='Blind Drive Report Card')
    parser.add_argument('input_file', nargs='?',
                        default='/data/data/com.termux/files/home/gojo/motion_tracker_sessions/full_drive_20251124.json.gz',
                        help='Path to stitched drive data')

    args = parser.parse_args()

    analyze_headless(args.input_file)
