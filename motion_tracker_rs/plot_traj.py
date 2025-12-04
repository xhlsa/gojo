#!/usr/bin/env python3
"""
Trajectory visualization for validating EKF predictions.
Plots 2D path (X-Y), altitude (Z), velocity profiles, and gravity well effectiveness.

Usage:
    python3 plot_traj.py                    # Use default trajectory.csv
    python3 plot_traj.py my_trajectory.csv  # Specify CSV file
"""

import pandas as pd
import matplotlib.pyplot as plt
import sys
import numpy as np

def plot_trajectory(csv_file="trajectory.csv"):
    """Plot trajectory from CSV export."""
    try:
        df = pd.read_csv(csv_file)
    except FileNotFoundError:
        print(f"Error: {csv_file} not found. Run the trajectory_prediction example first.")
        sys.exit(1)

    # Create 2x3 subplot grid for comprehensive analysis
    fig, axes = plt.subplots(2, 3, figsize=(18, 10))
    fig.suptitle('Predicted Trajectory Analysis - Gravity Well Validation', fontsize=16)

    # Plot 1: 2D Path (X-Y)
    ax1 = axes[0, 0]
    ax1.plot(df['px'], df['py'], 'b-', linewidth=2, label='Predicted Path')
    ax1.plot(df['px'].iloc[0], df['py'].iloc[0], 'go', markersize=10, label='Start')
    ax1.plot(df['px'].iloc[-1], df['py'].iloc[-1], 'ro', markersize=10, label='End')
    ax1.set_xlabel('X Position (m)')
    ax1.set_ylabel('Y Position (m)')
    ax1.set_title('2D Path (Top View)')
    ax1.grid(True, alpha=0.3)
    ax1.legend()
    ax1.axis('equal')

    # Plot 2: Altitude over time with drift analysis
    ax2 = axes[0, 1]
    z_drift = df['pz'] - df['pz'].iloc[0]
    ax2.plot(df['time'], z_drift, 'r-', linewidth=2, label='Z Drift')
    ax2.axhline(0, color='k', linestyle='--', alpha=0.3, label='Initial Level')

    # Highlight excessive drift zones
    excessive_drift_mask = np.abs(z_drift) > 0.5  # Flag >50cm drift
    if excessive_drift_mask.any():
        ax2.scatter(df['time'][excessive_drift_mask], z_drift[excessive_drift_mask],
                   color='orange', s=30, alpha=0.6, label='⚠️ Drift >0.5m', zorder=5)

    ax2.set_xlabel('Time (s)')
    ax2.set_ylabel('Altitude Drift (m)')
    ax2.set_title('Altitude Drift (Gravity Well Test)')
    ax2.grid(True, alpha=0.3)
    ax2.legend()

    # Plot 3: Z-velocity (Gravity Well effectiveness)
    ax3 = axes[0, 2]
    ax3.plot(df['time'], df['vz'], 'purple', linewidth=2, label='Vz (Vertical Velocity)')
    ax3.axhline(0, color='k', linestyle='--', alpha=0.3)
    ax3.fill_between(df['time'], -0.1, 0.1, color='green', alpha=0.2, label='Acceptable (<0.1 m/s)')
    ax3.set_xlabel('Time (s)')
    ax3.set_ylabel('Vertical Velocity (m/s)')
    ax3.set_title('Z-Velocity (Should Stay Near Zero)')
    ax3.grid(True, alpha=0.3)
    ax3.legend()

    # Plot 4: Velocity magnitude
    ax4 = axes[1, 0]
    v_mag = (df['vx']**2 + df['vy']**2 + df['vz']**2)**0.5
    v_xy = (df['vx']**2 + df['vy']**2)**0.5
    ax4.plot(df['time'], v_mag, 'b-', linewidth=2, label='Total Speed')
    ax4.plot(df['time'], v_xy, 'g--', linewidth=2, label='Horizontal Speed')
    ax4.plot(df['time'], df['vz'].abs(), 'r:', linewidth=2, label='|Vertical Speed|')
    ax4.set_xlabel('Time (s)')
    ax4.set_ylabel('Speed (m/s)')
    ax4.set_title('Velocity Profile')
    ax4.grid(True, alpha=0.3)
    ax4.legend()

    # Plot 5: Velocity components
    ax5 = axes[1, 1]
    ax5.plot(df['time'], df['vx'], 'r-', label='Vx (Forward)')
    ax5.plot(df['time'], df['vy'], 'g-', label='Vy (Lateral)')
    ax5.plot(df['time'], df['vz'], 'b-', label='Vz (Vertical)')
    ax5.axhline(0, color='k', linestyle='--', alpha=0.3)
    ax5.set_xlabel('Time (s)')
    ax5.set_ylabel('Velocity (m/s)')
    ax5.set_title('Velocity Components')
    ax5.grid(True, alpha=0.3)
    ax5.legend()

    # Plot 6: Gravity Well Diagnostic
    ax6 = axes[1, 2]
    # Calculate cumulative Z-drift rate (m/s averaged)
    z_drift_rate = np.gradient(z_drift, df['time'])
    ax6.plot(df['time'], z_drift_rate, 'orange', linewidth=2, label='Z-Drift Rate')
    ax6.axhline(0, color='k', linestyle='--', alpha=0.3)
    ax6.fill_between(df['time'], -0.01, 0.01, color='green', alpha=0.2, label='Excellent (<0.01 m/s)')
    ax6.set_xlabel('Time (s)')
    ax6.set_ylabel('Drift Rate (m/s)')
    ax6.set_title('Altitude Drift Rate (Lower=Better)')
    ax6.grid(True, alpha=0.3)
    ax6.legend()

    plt.tight_layout()
    plt.savefig('trajectory_plot.png', dpi=150)
    print(f"✓ Saved plot to trajectory_plot.png")

    # Print stats
    print(f"\n=== Trajectory Statistics ===")
    print(f"Duration: {df['time'].iloc[-1]:.2f} seconds")
    print(f"Total distance: {((df['px'].diff()**2 + df['py'].diff()**2)**0.5).sum():.2f} m")
    print(f"Max speed: {v_mag.max():.2f} m/s ({v_mag.max() * 3.6:.1f} km/h)")
    print(f"Mean horizontal speed: {v_xy.mean():.2f} m/s")

    # Gravity Well Effectiveness Analysis
    z_drift = df['pz'].iloc[-1] - df['pz'].iloc[0]
    z_drift_abs_max = np.abs(z_drift).max()
    z_vel_rms = np.sqrt(np.mean(df['vz']**2))
    z_vel_max = df['vz'].abs().max()

    print(f"\n=== Gravity Well Effectiveness ===")
    print(f"Total Z-drift: {z_drift:.3f} m")
    print(f"Max absolute Z-drift: {z_drift_abs_max:.3f} m")
    print(f"Z-velocity RMS: {z_vel_rms:.4f} m/s")
    print(f"Max Z-velocity: {z_vel_max:.3f} m/s")

    # Assess gravity well performance
    if z_drift_abs_max < 0.1 and z_vel_rms < 0.01:
        verdict = "✅ EXCELLENT (Gravity well working perfectly)"
    elif z_drift_abs_max < 0.5 and z_vel_rms < 0.05:
        verdict = "✅ GOOD (Minimal drift, gravity well effective)"
    elif z_drift_abs_max < 1.0:
        verdict = "⚠️  ACCEPTABLE (Some drift present, check for hills/ramps)"
    else:
        verdict = "❌ POOR (Excessive drift! Check Z-constraint parameters)"

    print(f"Verdict: {verdict}")

    # Warning for excessive drift
    if z_drift_abs_max > 0.5:
        print(f"\n⚠️  WARNING: Altitude drifted >{z_drift_abs_max:.2f}m")
        print("   This may indicate:")
        print("   1. Gravity well too weak (increase damping from 0.80 → 0.70)")
        print("   2. Actual elevation change (hill, overpass)")
        print("   3. Unestimated Z-axis accel bias")

    plt.show()

if __name__ == "__main__":
    if len(sys.argv) > 1:
        plot_trajectory(sys.argv[1])
    else:
        plot_trajectory()
