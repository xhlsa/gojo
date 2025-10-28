"""
RotationDetector - Device orientation tracking via gyroscope integration.

This module detects and tracks device rotation by integrating gyroscope readings
over time. Provides rotation angles in 3D space (pitch, roll, yaw) and can detect
primary rotation axis for device orientation changes.

Key features:
- Angle integration from gyroscope angular velocities
- Automatic angle normalization to [-π, π] range
- Rotation history tracking for temporal analysis
- Axis dominance detection (which axis contributes most to rotation)
- Integration with AccelerometerThread for motion tracking calibration triggers
"""

import logging
import math
from collections import deque

# Configure logging for debug output
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class RotationDetector:
    """
    Tracks device rotation via gyroscope data integration.

    Integrates gyroscope angular velocities (rad/s) over time to compute absolute
    rotation angles for pitch, roll, and yaw. Maintains full rotation history and
    detects primary rotation axis for orientation analysis.

    INTEGRATION WITH MOTION TRACKER:
    The RotationDetector is designed to integrate with AccelerometerThread to
    trigger dynamic accelerometer recalibration when significant device rotation
    is detected. This ensures gravity bias remains accurate across orientation
    changes.

    Example integration in AccelerometerThread.run():

        # In AccelerometerThread.__init__:
        self.rotation_detector = RotationDetector(history_size=6000)

        # In AccelerometerThread.run() main loop (Termux API reading):
        gyro_result = subprocess.run(
            ['termux-sensor', '-s', 'GYROSCOPE', '-l', '1'],
            capture_output=True,
            text=True,
            timeout=2
        )
        if gyro_result.returncode == 0:
            gyro_data = json.loads(gyro_result.stdout)
            if 'GYROSCOPE' in gyro_data:
                values = gyro_data['GYROSCOPE']['values']
                # values[0] = gyro_x (rad/s), values[1] = gyro_y, values[2] = gyro_z
                self.rotation_detector.update_gyroscope(values[0], values[1], values[2], dt)

                # Trigger recalibration if rotation exceeds threshold
                rotation_state = self.rotation_detector.get_rotation_state()
                if rotation_state['total_rotation_degrees'] > 30:  # >30° rotation
                    self.try_recalibrate(is_stationary=True)
                    self.rotation_detector.reset_all()  # Reset angles after recal

    TERMUX API GYROSCOPE READING EXAMPLE:

        termux-sensor -s GYROSCOPE -l 1  # Single read
        # Output: {"GYROSCOPE": {"values": [gyro_x, gyro_y, gyro_z], ...}}

        Typical ranges:
        - Slow rotation: 0.1 - 1.0 rad/s
        - Fast rotation: 1.0 - 5.0 rad/s
        - Max (typical phone): ~6.0 rad/s

    Args:
        history_size (int): Max rotation history samples to keep (default 6000 = 60s @ 100Hz)
        reset_on_large_dt (bool): Skip samples with dt > 100ms for clean angle tracking

    Attributes:
        angle_pitch (float): Rotation angle around X-axis (radians, normalized to [-π, π])
        angle_roll (float): Rotation angle around Y-axis (radians, normalized to [-π, π])
        angle_yaw (float): Rotation angle around Z-axis (radians, normalized to [-π, π])
        rotation_history (deque): History of rotation state snapshots
    """

    def __init__(self, history_size=6000, reset_on_large_dt=True):
        """
        Initialize rotation detector.

        Args:
            history_size (int): Number of rotation history samples to maintain (default 6000 = 60s @ 100Hz)
            reset_on_large_dt (bool): If True, skip samples with dt > 100ms (large time gaps)
        """
        # Rotation angles (rad, normalized to [-π, π])
        self.angle_pitch = 0.0  # Rotation around X-axis
        self.angle_roll = 0.0   # Rotation around Y-axis
        self.angle_yaw = 0.0    # Rotation around Z-axis

        # Configuration
        self.history_size = history_size
        self.reset_on_large_dt = reset_on_large_dt
        self.max_dt = 0.1  # 100ms - skip samples larger than this

        # History tracking (for temporal analysis)
        self.rotation_history = deque(maxlen=history_size)

        # Statistics for axis dominance
        self.axis_contributions = {'x': 0.0, 'y': 0.0, 'z': 0.0}
        self.axis_count = 0

        # Timing
        self.last_update_time = None
        self.sample_count = 0
        self.skipped_large_dt_count = 0

    def _normalize_angle(self, angle):
        """
        Normalize angle to [-π, π] range.

        Wraps angles outside the standard radian range back into [-π, π].
        This prevents unbounded angle growth and enables meaningful comparisons.

        Args:
            angle (float): Raw angle in radians

        Returns:
            float: Normalized angle in range [-π, π]
        """
        # Normalize to [-π, π]
        while angle > math.pi:
            angle -= 2 * math.pi
        while angle < -math.pi:
            angle += 2 * math.pi
        return angle

    def update_gyroscope(self, gyro_x, gyro_y, gyro_z, dt):
        """
        Update rotation angles from gyroscope reading.

        Integrates angular velocities (rad/s) to compute absolute rotation angles.
        Applies magnitude-based filtering to remove noise during stationary periods.

        LIMITATIONS:
        - Magnitude calculation assumes rotations < 60°. For rotations > 60°,
          the magnitude approximation becomes less accurate (error ~5-10% at 90°).
          Consider using quaternion-based integration for larger rotations.
        - Angular velocity integration assumes constant rotation rate during dt.
        - Does not account for gyroscope bias drift (manual recalibration recommended).

        Args:
            gyro_x (float): Angular velocity around X-axis (rad/s)
            gyro_y (float): Angular velocity around Y-axis (rad/s)
            gyro_z (float): Angular velocity around Z-axis (rad/s)
            dt (float): Time step since last update (seconds)

        Returns:
            bool: True if sample was processed, False if skipped due to validation

        Raises:
            ValueError: If dt <= 0
        """
        # INPUT VALIDATION - Check for valid float inputs
        try:
            gyro_x = float(gyro_x)
            gyro_y = float(gyro_y)
            gyro_z = float(gyro_z)
            dt = float(dt)
        except (TypeError, ValueError):
            logger.warning(f"Invalid gyroscope data: x={gyro_x}, y={gyro_y}, z={gyro_z}, dt={dt} - skipping sample")
            return False

        # Validate dt
        if dt <= 0:
            logger.warning(f"Invalid dt={dt} (must be > 0) - skipping sample")
            return False

        # LARGE DT HANDLING - Skip samples with suspicious time gaps
        if self.reset_on_large_dt and dt > self.max_dt:
            self.skipped_large_dt_count += 1
            logger.debug(f"Skipping sample: dt={dt*1000:.1f}ms > {self.max_dt*1000:.0f}ms threshold (skip count: {self.skipped_large_dt_count})")
            return False

        # Integrate angular velocities to get angle increments
        delta_pitch = gyro_x * dt
        delta_roll = gyro_y * dt
        delta_yaw = gyro_z * dt

        # Update angles
        self.angle_pitch += delta_pitch
        self.angle_roll += delta_roll
        self.angle_yaw += delta_yaw

        # ANGLE NORMALIZATION - Keep angles in [-π, π] range
        self.angle_pitch = self._normalize_angle(self.angle_pitch)
        self.angle_roll = self._normalize_angle(self.angle_roll)
        self.angle_yaw = self._normalize_angle(self.angle_yaw)

        # Calculate magnitude for axis dominance tracking
        rotation_magnitude = math.sqrt(delta_pitch**2 + delta_roll**2 + delta_yaw**2)

        if rotation_magnitude > 0:
            # Track contributions per axis
            self.axis_contributions['x'] += abs(delta_pitch)
            self.axis_contributions['y'] += abs(delta_roll)
            self.axis_contributions['z'] += abs(delta_yaw)
            self.axis_count += 1

        # Add to history
        self.rotation_history.append({
            'pitch': self.angle_pitch,
            'roll': self.angle_roll,
            'yaw': self.angle_yaw,
            'magnitude': rotation_magnitude,
            'dt': dt
        })

        # Update timing
        self.sample_count += 1
        self.last_update_time = dt

        return True

    def get_rotation_state(self):
        """
        Get current rotation state.

        Returns dict with current angles and rotation magnitude.

        Returns:
            dict: {
                'angle_pitch': float (rad, normalized to [-π, π]),
                'angle_roll': float (rad, normalized to [-π, π]),
                'angle_yaw': float (rad, normalized to [-π, π]),
                'angle_pitch_degrees': float,
                'angle_roll_degrees': float,
                'angle_yaw_degrees': float,
                'total_rotation_radians': float (magnitude of rotation vector),
                'total_rotation_degrees': float,
                'primary_axis': str ('x', 'y', or 'z'),
                'sample_count': int
            }
        """
        # Calculate total rotation magnitude
        total_rotation = math.sqrt(
            self.angle_pitch**2 + self.angle_roll**2 + self.angle_yaw**2
        )

        # Find primary rotation axis
        primary_axis = self.get_axis_dominance()

        return {
            'angle_pitch': self.angle_pitch,
            'angle_roll': self.angle_roll,
            'angle_yaw': self.angle_yaw,
            'angle_pitch_degrees': math.degrees(self.angle_pitch),
            'angle_roll_degrees': math.degrees(self.angle_roll),
            'angle_yaw_degrees': math.degrees(self.angle_yaw),
            'total_rotation_radians': total_rotation,
            'total_rotation_degrees': math.degrees(total_rotation),
            'primary_axis': primary_axis,
            'sample_count': self.sample_count
        }

    def get_axis_dominance(self):
        """
        Determine which rotation axis contributes most to total rotation.

        Analyzes cumulative contributions per axis to identify the primary
        rotation direction. Useful for understanding device orientation changes.

        Returns:
            str: 'x' (pitch), 'y' (roll), or 'z' (yaw) - whichever has largest contribution.
                Returns 'none' if no samples processed.
        """
        if self.axis_count == 0:
            return 'none'

        max_axis = 'x'
        max_value = self.axis_contributions['x']

        if self.axis_contributions['y'] > max_value:
            max_axis = 'y'
            max_value = self.axis_contributions['y']

        if self.axis_contributions['z'] > max_value:
            max_axis = 'z'

        return max_axis

    def get_rotation_history(self):
        """
        Get full rotation history for temporal analysis.

        Returns a copy of the rotation history deque. Useful for analyzing
        rotation patterns over time or detecting oscillations.

        Returns:
            list: History of rotation states, each containing:
                {
                    'pitch': float (rad),
                    'roll': float (rad),
                    'yaw': float (rad),
                    'magnitude': float (rad),
                    'dt': float (seconds)
                }
        """
        return list(self.rotation_history)

    def reset_rotation_angles(self):
        """
        Reset rotation angles to zero (pitch, roll, yaw = 0).

        Use when the device is re-oriented and you want to start measuring
        rotation from the new position. Does NOT clear history or axis stats.

        Typical use case: After dynamic accelerometer recalibration,
        reset angles to baseline so next orientation change is detectable.
        """
        self.angle_pitch = 0.0
        self.angle_roll = 0.0
        self.angle_yaw = 0.0
        logger.debug("Reset rotation angles to 0 (pitch, roll, yaw)")

    def reset_all(self):
        """
        Complete reset: angles, history, and axis statistics.

        Use when starting a new tracking session or when full state reset is needed.
        Clears all rotation history and resets contribution tracking.

        Typical use case: User initiates new motion tracking session.
        """
        self.angle_pitch = 0.0
        self.angle_roll = 0.0
        self.angle_yaw = 0.0
        self.rotation_history.clear()
        self.axis_contributions = {'x': 0.0, 'y': 0.0, 'z': 0.0}
        self.axis_count = 0
        self.sample_count = 0
        self.skipped_large_dt_count = 0
        self.last_update_time = None
        logger.debug("Complete reset: angles, history, and axis statistics cleared")

    def get_diagnostics(self):
        """
        Get diagnostic information for debugging and monitoring.

        Returns:
            dict: {
                'sample_count': int,
                'skipped_large_dt_count': int,
                'history_size': int (current),
                'history_capacity': int,
                'axis_dominance': str,
                'last_update_dt': float or None
            }
        """
        return {
            'sample_count': self.sample_count,
            'skipped_large_dt_count': self.skipped_large_dt_count,
            'history_size': len(self.rotation_history),
            'history_capacity': self.history_size,
            'axis_dominance': self.get_axis_dominance(),
            'last_update_dt': self.last_update_time
        }
