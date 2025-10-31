"""
Real-time metrics collector for 13D Gyro-EKF validation.

Tracks key metrics to validate the filter is:
1. Learning gyro bias correctly
2. Keeping quaternion healthy
3. Converging to correct heading
4. Reducing position/velocity error
5. Detecting incidents reliably
"""

import numpy as np
import math
import time
from collections import deque
from statistics import mean, stdev


class MetricsCollector:
    """Collect and validate 13D EKF metrics in real-time."""

    def __init__(self, max_history=600):
        """
        Initialize metrics collector.

        Args:
            max_history: Max samples to keep (600 = ~2 min at 5 Hz)
        """
        self.start_time = time.time()
        self.max_history = max_history

        # Core metric histories
        self.bias_magnitude = deque(maxlen=max_history)
        self.quaternion_norm = deque(maxlen=max_history)
        self.quaternion_rate = deque(maxlen=max_history)
        self.heading_error = deque(maxlen=max_history)  # EKF heading vs GPS heading
        self.innovation_magnitude = deque(maxlen=max_history)
        self.gyro_residual = deque(maxlen=max_history)  # (gyro - bias) magnitude
        self.velocity_magnitude = deque(maxlen=max_history)

        # Incident detection
        self.pitch_angles = deque(maxlen=max_history)
        self.yaw_rates = deque(maxlen=max_history)
        self.hard_braking_count = 0
        self.swerving_count = 0

        # Summary stats
        self.last_status_time = 0
        self.last_gps_heading = None
        self.convergence_time = None

    def update(self, ekf_state, gyro_measurement, gps_heading=None, accel_magnitude=0):
        """
        Update metrics from EKF state and measurements.

        Args:
            ekf_state: dict from ekf.get_state() with 'quaternion', 'gyro_bias', etc.
            gyro_measurement: [gx, gy, gz] raw gyroscope measurement
            gps_heading: heading from GPS (degrees), if available
            accel_magnitude: magnitude of acceleration (m/s²)
        """
        now = time.time() - self.start_time

        # Extract state components
        q = ekf_state.get('quaternion', [1, 0, 0, 0])
        bias = ekf_state.get('gyro_bias', [0, 0, 0])

        # === BIAS CONVERGENCE METRICS ===
        bias_mag = np.linalg.norm(bias)
        self.bias_magnitude.append(bias_mag)

        # Detect convergence (first time bias crosses 0.001 rad/s threshold)
        if self.convergence_time is None and bias_mag > 0.001:
            self.convergence_time = now

        # === QUATERNION HEALTH METRICS ===
        q_norm = np.linalg.norm(q)
        self.quaternion_norm.append(q_norm)

        # Quaternion rate (magnitude of dq from gyro)
        gyro_mag = np.linalg.norm(gyro_measurement)
        q_rate = 0.5 * gyro_mag  # dq/dt ≈ 0.5 * |ω|
        self.quaternion_rate.append(q_rate)

        # Extract Euler angles from quaternion for pitch/yaw/roll
        roll, pitch, yaw = self.quaternion_to_euler(q)
        self.pitch_angles.append(pitch)
        self.yaw_rates.append(gyro_measurement[2] if len(gyro_measurement) > 2 else 0)

        # === GYRO RESIDUAL (bias-corrected gyro) ===
        residual = np.linalg.norm(np.array(gyro_measurement) - np.array(bias))
        self.gyro_residual.append(residual)

        # === HEADING COMPARISON (GPS vs EKF) ===
        if gps_heading is not None:
            self.last_gps_heading = gps_heading
            ekf_heading = math.degrees(yaw)
            # Normalize to 0-360
            heading_diff = abs((ekf_heading - gps_heading + 180) % 360 - 180)
            self.heading_error.append(heading_diff)

        # === VELOCITY TRACKING ===
        vel = ekf_state.get('velocity', 0)
        self.velocity_magnitude.append(vel)

        # === INCIDENT DETECTION VALIDATION ===
        # Hard braking: pitch angle < -10° and accel > 0.8g
        if pitch < -10 and accel_magnitude > 0.8:
            self.hard_braking_count += 1

        # Swerving: yaw rate > 60°/sec (1.047 rad/sec)
        if abs(gyro_measurement[2]) > 1.047:
            self.swerving_count += 1

    def get_summary(self):
        """
        Get current metrics summary.

        Returns:
            dict with key metrics and health status
        """
        if not self.bias_magnitude:
            return None

        now = time.time() - self.start_time

        # Bias stats
        bias_current = self.bias_magnitude[-1]
        bias_history = list(self.bias_magnitude)
        bias_converged = (
            self.convergence_time is not None and
            (now - self.convergence_time) > 30
        )

        if len(bias_history) > 30:
            bias_std = stdev(bias_history[-30:])  # Last 30 samples
        else:
            bias_std = 0

        # Quaternion health
        q_norm_current = self.quaternion_norm[-1]
        q_norm_error = abs(q_norm_current - 1.0)
        q_norm_healthy = q_norm_error < 0.001

        # Heading convergence
        heading_errors = list(self.heading_error)
        heading_converged = (
            len(heading_errors) > 30 and
            mean(heading_errors[-30:]) < 30  # Last 30 samples < 30° error
        )

        # Innovation (gyro residual - should be small when bias correct)
        residuals = list(self.gyro_residual)
        if len(residuals) > 30:
            residual_mean = mean(residuals[-30:])
            residual_std = stdev(residuals[-30:])
        else:
            residual_mean = residuals[-1] if residuals else 0
            residual_std = 0

        # Overall health
        issues = []
        if bias_current < 0.0001 and now > 60:
            issues.append("BIAS_NOT_LEARNING")
        if bias_current > 0.1:
            issues.append("BIAS_TOO_HIGH")
        if not q_norm_healthy:
            issues.append("QUAT_DENORMALIZED")
        if residual_mean > 0.05:
            issues.append("GYRO_RESIDUAL_HIGH")

        return {
            'elapsed_time': now,
            'bias_current': bias_current,
            'bias_converged': bias_converged,
            'bias_convergence_time': self.convergence_time,
            'bias_stability_std': bias_std,
            'quaternion_norm': q_norm_current,
            'quaternion_norm_error': q_norm_error,
            'quaternion_healthy': q_norm_healthy,
            'heading_converged': heading_converged,
            'heading_error_mean': mean(heading_errors) if heading_errors else None,
            'gyro_residual_mean': residual_mean,
            'gyro_residual_std': residual_std,
            'hard_braking_detected': self.hard_braking_count,
            'swerving_detected': self.swerving_count,
            'issues': issues,
            'status': 'HEALTHY' if not issues else 'WARNING'
        }

    def print_dashboard(self, interval=5):
        """
        Print real-time metrics dashboard.

        Args:
            interval: Print every N seconds
        """
        now = time.time()
        if now - self.last_status_time < interval:
            return

        self.last_status_time = now
        summary = self.get_summary()
        if not summary:
            return

        elapsed = summary['elapsed_time']
        print(f"\n[{int(elapsed//60):02d}:{int(elapsed%60):02d}] 13D GYRO-EKF METRICS")
        print("=" * 75)

        # Bias convergence
        bias = summary['bias_current']
        bias_status = "✓ CONVERGING" if summary['bias_converged'] else "⏳ Learning..."
        print(f"Bias Magnitude:      {bias:.6f} rad/s  [{bias_status}]")
        if summary['bias_convergence_time']:
            print(f"  └─ Converged at: {summary['bias_convergence_time']:.1f}s")
        print(f"  └─ Stability (σ):  {summary['bias_stability_std']:.6f} rad/s")

        # Quaternion health
        q_norm = summary['quaternion_norm']
        q_status = "✓ HEALTHY" if summary['quaternion_healthy'] else "✗ ERROR"
        print(f"\nQuaternion Norm:     {q_norm:.6f}  [{q_status}]")
        print(f"  └─ Error:          {summary['quaternion_norm_error']:.6f}")

        # Heading convergence
        if summary['heading_error_mean'] is not None:
            h_err = summary['heading_error_mean']
            h_status = "✓ CONVERGED" if summary['heading_converged'] else "⏳ Converging..."
            print(f"\nHeading Error (GPS): {h_err:.1f}°  [{h_status}]")

        # Gyro residual (bias-corrected gyro magnitude)
        print(f"\nGyro Residual:       {summary['gyro_residual_mean']:.6f} rad/s")
        print(f"  └─ Std Dev:        {summary['gyro_residual_std']:.6f} rad/s")

        # Incident detection
        print(f"\nIncident Detection:")
        print(f"  Hard Braking:      {summary['hard_braking_detected']:3d} events")
        print(f"  Swerving:          {summary['swerving_detected']:3d} events")

        # Status
        status = summary['status']
        status_emoji = "✓" if status == "HEALTHY" else "⚠"
        print(f"\nOverall Status:      {status_emoji} {status}")
        if summary['issues']:
            print(f"  Issues: {', '.join(summary['issues'])}")

        print("=" * 75)

    @staticmethod
    def quaternion_to_euler(q):
        """
        Convert quaternion [q0, q1, q2, q3] to Euler angles [roll, pitch, yaw].

        Returns: (roll_rad, pitch_rad, yaw_rad)
        """
        q0, q1, q2, q3 = q

        # Roll (X-axis rotation)
        roll = math.atan2(2*(q0*q1 + q2*q3), 1 - 2*(q1**2 + q2**2))

        # Pitch (Y-axis rotation)
        pitch = math.asin(2*(q0*q2 - q3*q1))

        # Yaw (Z-axis rotation)
        yaw = math.atan2(2*(q0*q3 + q1*q2), 1 - 2*(q2**2 + q3**2))

        return roll, pitch, yaw

    def export_metrics(self, filename):
        """
        Export full metric history to JSON file.

        Args:
            filename: Path to output JSON file
        """
        import json

        data = {
            'bias_magnitude': list(self.bias_magnitude),
            'quaternion_norm': list(self.quaternion_norm),
            'quaternion_rate': list(self.quaternion_rate),
            'heading_error': list(self.heading_error),
            'gyro_residual': list(self.gyro_residual),
            'pitch_angles': list(self.pitch_angles),
            'yaw_rates': list(self.yaw_rates),
            'velocity_magnitude': list(self.velocity_magnitude),
            'hard_braking_count': self.hard_braking_count,
            'swerving_count': self.swerving_count,
            'convergence_time': self.convergence_time,
        }

        with open(filename, 'w') as f:
            json.dump(data, f, indent=2)
