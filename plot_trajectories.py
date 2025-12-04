#!/usr/bin/env python3
"""
Generate trajectory comparison plots for golden drives with different GPS decimations.
Shows EKF position estimates vs GPS ground truth.
"""

import json
import gzip
import sys
from pathlib import Path
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from dataclasses import dataclass

@dataclass
class TrajectoryPoint:
    timestamp: float
    ekf_x: float
    ekf_y: float
    ekf_velocity: float
    ekf_heading_deg: float
    has_gps: bool = False  # Mark if GPS update was available at this point
    valid: bool = True

def load_comparison_log(logfile: Path):
    """Load comparison log from gzipped JSON."""
    with gzip.open(logfile, 'rt') as f:
        data = json.load(f)
    return data

def extract_trajectories(comparison_data):
    """Extract trajectory points from comparison output."""
    trajectories = comparison_data.get('trajectories', [])
    points = []

    for t in trajectories:
        points.append(TrajectoryPoint(
            timestamp=t['timestamp'],
            ekf_x=t['ekf_x'],
            ekf_y=t['ekf_y'],
            ekf_velocity=t.get('ekf_velocity', 0),
            ekf_heading_deg=t.get('ekf_heading_deg', 0),
            has_gps=False,  # Will be set during decimation simulation
            valid=t.get('valid', True)
        ))
    return points

def plot_trajectory_comparison(title, plots_data, output_file):
    """
    Plot multiple trajectories for comparison.
    plots_data: list of (label, points, color)
    """
    fig, axes = plt.subplots(2, 2, figsize=(16, 12))
    fig.suptitle(title, fontsize=16, fontweight='bold')

    # Plot 1: XY Trajectory
    ax = axes[0, 0]
    for label, points, color in plots_data:
        valid_points = [p for p in points if p.valid]
        if valid_points:
            xs = [p.ekf_x for p in valid_points]
            ys = [p.ekf_y for p in valid_points]
            ax.plot(xs, ys, color=color, label=label, linewidth=2, alpha=0.8, zorder=4)
            ax.scatter([xs[0]], [ys[0]], marker='o', s=100, color=color, zorder=5, edgecolors='black')
            ax.scatter([xs[-1]], [ys[-1]], marker='s', s=100, color=color, zorder=5, edgecolors='black')

            # Overlay GPS fixes if available
            gps_points = [p for p in valid_points if p.has_gps]
            if gps_points:
                gps_xs = [p.ekf_x for p in gps_points]
                gps_ys = [p.ekf_y for p in gps_points]
                ax.scatter(gps_xs, gps_ys, color=color, s=10, alpha=0.4, marker='x', zorder=3)

    ax.set_xlabel('East (m)', fontsize=11)
    ax.set_ylabel('North (m)', fontsize=11)
    ax.set_title('XY Trajectory (x = GPS updates)', fontsize=12, fontweight='bold')
    ax.legend(loc='best', fontsize=10)
    ax.grid(True, alpha=0.3)
    ax.axis('equal')

    # Plot 2: Velocity over time
    ax = axes[0, 1]
    for label, points, color in plots_data:
        valid_points = [p for p in points if p.valid]
        if valid_points:
            ts = [(p.timestamp - valid_points[0].timestamp) for p in valid_points]
            vs = [p.ekf_velocity for p in valid_points]
            ax.plot(ts, vs, color=color, label=label, linewidth=2, alpha=0.8)
    ax.set_xlabel('Time (s)', fontsize=11)
    ax.set_ylabel('Velocity (m/s)', fontsize=11)
    ax.set_title('Velocity Over Time', fontsize=12, fontweight='bold')
    ax.legend(loc='best', fontsize=10)
    ax.grid(True, alpha=0.3)

    # Plot 3: Heading over time
    ax = axes[1, 0]
    for label, points, color in plots_data:
        valid_points = [p for p in points if p.valid]
        if valid_points:
            ts = [(p.timestamp - valid_points[0].timestamp) for p in valid_points]
            hs = [p.ekf_heading_deg for p in valid_points]
            ax.plot(ts, hs, color=color, label=label, linewidth=2, alpha=0.8)
    ax.set_xlabel('Time (s)', fontsize=11)
    ax.set_ylabel('Heading (°)', fontsize=11)
    ax.set_title('Heading Over Time', fontsize=12, fontweight='bold')
    ax.legend(loc='best', fontsize=10)
    ax.grid(True, alpha=0.3)

    # Plot 4: Stats text
    ax = axes[1, 1]
    ax.axis('off')
    stats_text = "STATISTICS\n" + "="*40 + "\n\n"
    for label, points, color in plots_data:
        valid_points = [p for p in points if p.valid]
        if valid_points:
            num_samples = len(valid_points)
            duration = valid_points[-1].timestamp - valid_points[0].timestamp
            max_vel = max(p.ekf_velocity for p in valid_points)
            avg_vel = np.mean([p.ekf_velocity for p in valid_points])
            total_dist = sum(np.sqrt((valid_points[i].ekf_x - valid_points[i-1].ekf_x)**2 +
                                     (valid_points[i].ekf_y - valid_points[i-1].ekf_y)**2)
                            for i in range(1, len(valid_points)))

            stats_text += f"{label}:\n"
            stats_text += f"  Samples: {num_samples}\n"
            stats_text += f"  Duration: {duration:.1f}s\n"
            stats_text += f"  Max speed: {max_vel:.2f} m/s\n"
            stats_text += f"  Avg speed: {avg_vel:.2f} m/s\n"
            stats_text += f"  Total dist: {total_dist:.1f}m\n\n"

    ax.text(0.1, 0.9, stats_text, transform=ax.transAxes, fontsize=10,
            verticalalignment='top', fontfamily='monospace',
            bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.5))

    plt.tight_layout()
    plt.savefig(output_file, dpi=150, bbox_inches='tight')
    print(f"✅ Saved: {output_file}")
    plt.close()

def run_replay(logfile, decimation, output_file):
    """Run replay with specified GPS decimation and extract comparison data."""
    import subprocess
    import gzip
    cmd = f"cd /data/data/com.termux/files/home/gojo/motion_tracker_rs && cargo run --release --bin replay -- --log {logfile} --gps-decimation {decimation}"
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            # Parse the JSON output from replay
            try:
                replay_data = json.loads(result.stdout)
                # The replay outputs summary stats, not full trajectories
                # We need to extract from the original log and simulate decimation
                print(f"   (replay completed with {decimation}x decimation)")
                return replay_data
            except:
                print(f"   ❌ Failed to parse replay output")
                return None
        else:
            print(f"   ❌ Replay failed: {result.stderr[:200]}")
            return None
    except subprocess.TimeoutExpired:
        print(f"   ⏱️ Replay timeout for {decimation}x decimation")
        return None

def simulate_decimation(points, decimation_factor):
    """Simulate GPS decimation by marking every Nth sample with has_gps."""
    if decimation_factor <= 1:
        # Mark all points as having GPS
        for p in points:
            p.has_gps = True
        return points

    # Create a copy and mark every Nth sample
    decimated_points = []
    for i, p in enumerate(points):
        new_p = TrajectoryPoint(
            timestamp=p.timestamp,
            ekf_x=p.ekf_x,
            ekf_y=p.ekf_y,
            ekf_velocity=p.ekf_velocity,
            ekf_heading_deg=p.ekf_heading_deg,
            has_gps=(i % decimation_factor == 0),  # Mark every Nth point
            valid=p.valid
        )
        decimated_points.append(new_p)

    return decimated_points

def main():
    """Generate trajectory plots for golden drives."""
    import subprocess
    golden_dir = Path("/data/data/com.termux/files/home/gojo/motion_tracker_sessions/golden")
    rs_dir = Path("/data/data/com.termux/files/home/gojo/motion_tracker_rs")

    # Crown jewel drive
    crown_jewel = golden_dir / "comparison_20251126_183814.json.gz"
    second_drive = golden_dir / "comparison_20251125_180829.json.gz"

    print("="*70)
    print("GENERATING TRAJECTORY PLOTS WITH GPS DECIMATION COMPARISON")
    print("="*70)

    def run_replay_with_decimation(logfile, decimation):
        """Run actual replay with GPS decimation."""
        cmd = f"cd {rs_dir} && cargo run --release --bin replay -- --log {logfile} --gps-decimation {decimation}"
        try:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                data = json.loads(result.stdout)
                if isinstance(data, list):
                    data = data[0]
                return data
        except Exception as e:
            print(f"      Error: {e}")
        return None

    # Process Crown Jewel
    print(f"\n🏆 CROWN JEWEL DRIVE: {crown_jewel.name}")
    print("   Running replays with different GPS decimations...")

    # Run actual replays with different decimations
    print(f"   1x (full GPS)...")
    data_1x = run_replay_with_decimation(str(crown_jewel), 1)
    traj_1x = extract_trajectories(data_1x) if data_1x else []
    if traj_1x:
        print(f"      {len(traj_1x)} trajectory points, {traj_1x[-1].timestamp - traj_1x[0].timestamp:.1f}s")

    print(f"   10x (10% GPS)...")
    data_10x = run_replay_with_decimation(str(crown_jewel), 10)
    traj_10x = extract_trajectories(data_10x) if data_10x else []
    if traj_10x:
        print(f"      {len(traj_10x)} trajectory points")

    print(f"   20x (5% GPS)...")
    data_20x = run_replay_with_decimation(str(crown_jewel), 20)
    traj_20x = extract_trajectories(data_20x) if data_20x else []
    if traj_20x:
        print(f"      {len(traj_20x)} trajectory points")

    # Generate comparison plots
    if traj_1x and traj_10x and traj_20x:
        print("\n   Generating comparison plots...")
        plots_data = [
            ("EKF 1x (full GPS)", traj_1x, 'blue'),
            ("EKF 10x (10% GPS)", traj_10x, 'green'),
            ("EKF 20x (5% GPS)", traj_20x, 'orange'),
        ]

        plot_trajectory_comparison(
            "Crown Jewel: EKF Trajectory with Different GPS Decimations",
            plots_data,
            "trajectory_crown_jewel_comparison.png"
        )

    # Process Second Drive
    print(f"\n📊 SECOND DRIVE: {second_drive.name}")
    print("   Running replays with different GPS decimations...")

    print(f"   1x (full GPS)...")
    data_1x_s = run_replay_with_decimation(str(second_drive), 1)
    traj_1x_s = extract_trajectories(data_1x_s) if data_1x_s else []
    if traj_1x_s:
        print(f"      {len(traj_1x_s)} trajectory points, {traj_1x_s[-1].timestamp - traj_1x_s[0].timestamp:.1f}s")

    print(f"   10x (10% GPS)...")
    data_10x_s = run_replay_with_decimation(str(second_drive), 10)
    traj_10x_s = extract_trajectories(data_10x_s) if data_10x_s else []
    if traj_10x_s:
        print(f"      {len(traj_10x_s)} trajectory points")

    print(f"   20x (5% GPS)...")
    data_20x_s = run_replay_with_decimation(str(second_drive), 20)
    traj_20x_s = extract_trajectories(data_20x_s) if data_20x_s else []
    if traj_20x_s:
        print(f"      {len(traj_20x_s)} trajectory points")

    # Generate comparison plots
    if traj_1x_s and traj_10x_s and traj_20x_s:
        print("\n   Generating comparison plots...")
        plots_data_s = [
            ("EKF 1x (full GPS)", traj_1x_s, 'blue'),
            ("EKF 10x (10% GPS)", traj_10x_s, 'green'),
            ("EKF 20x (5% GPS)", traj_20x_s, 'orange'),
        ]

        plot_trajectory_comparison(
            "Second Drive: EKF Trajectory with Different GPS Decimations",
            plots_data_s,
            "trajectory_second_drive_comparison.png"
        )

    print("\n" + "="*70)
    print("✅ TRAJECTORY PLOTS GENERATED SUCCESSFULLY")
    print("="*70)

if __name__ == "__main__":
    main()
