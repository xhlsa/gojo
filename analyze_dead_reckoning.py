#!/usr/bin/env python3
"""
Analyze pure dead reckoning performance by comparing EKF trajectory
against GPS ground truth over the full drive.
"""

import json
import gzip
import sys
from pathlib import Path
import numpy as np
import matplotlib.pyplot as plt

def load_comparison_log(logfile: Path):
    """Load comparison log from gzipped JSON."""
    with gzip.open(logfile, 'rt') as f:
        data = json.load(f)
    return data

def latlon_to_local(lat, lon, ref_lat, ref_lon):
    """Convert lat/lon to local ENU coordinates (meters)."""
    R_earth = 6371000  # meters
    dlat = np.radians(lat - ref_lat)
    dlon = np.radians(lon - ref_lon)
    x = R_earth * dlon * np.cos(np.radians(ref_lat))
    y = R_earth * dlat
    return x, y

def main():
    golden_dir = Path("/data/data/com.termux/files/home/gojo/motion_tracker_sessions/golden")
    crown_jewel = golden_dir / "comparison_20251126_183814.json.gz"

    print("Loading golden drive...")
    data = load_comparison_log(crown_jewel)
    traj = data.get('trajectories', [])
    print(f"Loaded {len(traj)} trajectory points")

    # Use first GPS as reference and origin
    if not traj or not traj[0].get('lat'):
        print("❌ No GPS data in log")
        return

    ref_lat, ref_lon = traj[0]['lat'], traj[0]['lon']
    print(f"Reference origin: {ref_lat:.6f}, {ref_lon:.6f}")

    # Extract EKF positions and GPS positions
    ekf_x = []
    ekf_y = []
    gps_x = []
    gps_y = []
    timestamps = []
    has_gps = []

    for i, t in enumerate(traj):
        if t.get('valid', True):
            ekf_x.append(t['ekf_x'])
            ekf_y.append(t['ekf_y'])
            timestamps.append(t['timestamp'])

            # Convert GPS to local coords
            if t.get('lat') and t.get('lon'):
                x, y = latlon_to_local(t['lat'], t['lon'], ref_lat, ref_lon)
                gps_x.append(x)
                gps_y.append(y)
                has_gps.append(True)
            else:
                gps_x.append(None)
                gps_y.append(None)
                has_gps.append(False)

    # Calculate position error over time
    errors = []
    gps_count = 0
    for i in range(len(ekf_x)):
        if has_gps[i] and gps_x[i] is not None:
            dx = ekf_x[i] - gps_x[i]
            dy = ekf_y[i] - gps_y[i]
            error = np.sqrt(dx*dx + dy*dy)
            errors.append(error)
            gps_count += 1

    print(f"\nWith full GPS ({gps_count} fixes):")
    if errors:
        print(f"  Position RMSE: {np.sqrt(np.mean(np.array(errors)**2)):.2f} m")
        print(f"  Max error: {np.max(errors):.2f} m")
        print(f"  Mean error: {np.mean(errors):.2f} m")
        print(f"  95th percentile: {np.percentile(errors, 95):.2f} m")

    # Now simulate pure dead reckoning: only trust first GPS, measure drift after
    print(f"\nPure dead reckoning analysis:")
    print(f"  Simulation starts at t={timestamps[0]:.1f}")
    print(f"  Drive duration: {timestamps[-1] - timestamps[0]:.1f} seconds")

    if gps_count < 2:
        print("  ⚠️ Insufficient GPS data for comparison")
        return

    # Find position when we have GPS data (should be ~zero initially)
    init_ekf_x = ekf_x[0]
    init_ekf_y = ekf_y[0]
    init_gps_x = gps_x[0]
    init_gps_y = gps_y[0]

    print(f"\n  Initial position (from 1st GPS):")
    print(f"    GPS: ({init_gps_x:.1f}, {init_gps_y:.1f}) m")
    print(f"    EKF: ({init_ekf_x:.1f}, {init_ekf_y:.1f}) m")

    # Calculate drift at different time intervals
    gps_indices = [i for i, has in enumerate(has_gps) if has]
    time_deltas = [30, 60, 120, 300, 600]  # seconds
    t_start = timestamps[gps_indices[0]]

    print(f"\n  Position drift vs time (from first GPS fix):")
    for dt in time_deltas:
        t_target = t_start + dt
        # Find closest point
        idx = min(range(len(timestamps)), key=lambda i: abs(timestamps[i] - t_target))
        if idx < len(timestamps):
            actual_dt = timestamps[idx] - t_start
            if actual_dt > 0 and idx < len(gps_x) and gps_x[idx] is not None:
                # Only count if we have GPS ground truth at this point
                gps_pos = (gps_x[idx], gps_y[idx])
                ekf_pos = (ekf_x[idx], ekf_y[idx])
                drift = np.sqrt((ekf_pos[0] - gps_pos[0])**2 + (ekf_pos[1] - gps_pos[1])**2)
                print(f"    +{actual_dt:.0f}s: {drift:.1f} m drift")

    # Create visualization
    fig, axes = plt.subplots(1, 2, figsize=(16, 6))
    fig.suptitle('Pure Dead Reckoning Analysis: Crown Jewel Drive', fontsize=14, fontweight='bold')

    # Plot 1: XY trajectory
    ax = axes[0]
    ax.plot(ekf_x, ekf_y, 'b-', linewidth=2, label='EKF Trajectory', alpha=0.8)
    ax.scatter(gps_x, gps_y, c='red', s=5, alpha=0.3, label=f'GPS Ground Truth ({gps_count} fixes)')
    ax.scatter([ekf_x[0]], [ekf_y[0]], marker='o', s=100, color='green', zorder=5, edgecolors='black', label='Start')
    ax.scatter([ekf_x[-1]], [ekf_y[-1]], marker='s', s=100, color='red', zorder=5, edgecolors='black', label='End')
    ax.set_xlabel('East (m)')
    ax.set_ylabel('North (m)')
    ax.set_title('XY Trajectory Comparison')
    ax.legend()
    ax.grid(True, alpha=0.3)
    ax.axis('equal')

    # Plot 2: Position error over time
    ax = axes[1]
    error_times = [timestamps[i] - t_start for i in gps_indices]
    error_vals = []
    for i in gps_indices:
        dx = ekf_x[i] - gps_x[i]
        dy = ekf_y[i] - gps_y[i]
        error_vals.append(np.sqrt(dx*dx + dy*dy))

    ax.scatter(error_times, error_vals, c='darkblue', s=20, alpha=0.6)
    ax.plot(error_times, error_vals, 'b-', alpha=0.3, linewidth=1)
    ax.axhline(y=np.mean(error_vals), color='r', linestyle='--', label=f'Mean: {np.mean(error_vals):.1f}m')
    ax.set_xlabel('Time since start (s)')
    ax.set_ylabel('Position Error (m)')
    ax.set_title('EKF vs GPS Position Error')
    ax.legend()
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig('dead_reckoning_analysis.png', dpi=150, bbox_inches='tight')
    print(f"\n✅ Saved: dead_reckoning_analysis.png")

if __name__ == "__main__":
    main()
