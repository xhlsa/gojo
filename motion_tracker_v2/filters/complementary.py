"""
Complementary filter for GPS + accelerometer sensor fusion.

Uses a weighted combination of GPS (ground truth, low frequency) and accelerometer
(high frequency detail) to produce accurate velocity and distance estimates.

GPS weight: 70% (accurate but updates ~1/sec)
Accel weight: 30% (noisy but updates 50+ times/sec)
"""

import math
import threading
import time
from collections import deque
from .base import SensorFusionBase
from .utils import haversine_distance


class ComplementaryFilter(SensorFusionBase):
    """
    Complementary filter fusing GPS and accelerometer data.

    GPS provides ground truth but low frequency updates.
    Accelerometer provides high frequency but drifts over time.
    Weighted fusion gives best of both: accurate + responsive.
    """

    def __init__(self, gps_weight=0.7, accel_weight=0.3):
        """
        Initialize complementary filter.

        Args:
            gps_weight (float): Weight for GPS updates (default 0.7)
            accel_weight (float): Weight for accel updates (default 0.3)
        """
        # Fusion weights (should sum to 1.0)
        self.gps_weight = gps_weight
        self.accel_weight = accel_weight

        # State variables
        self.velocity = 0.0  # m/s
        self.distance = 0.0  # meters
        self.last_time = None

        # GPS state
        self.last_gps_position = None
        self.last_gps_speed = None
        self.last_gps_time = None

        # Accelerometer state
        self.accel_velocity = 0.0
        self.accel_magnitude = 0.0  # Current acceleration magnitude
        self.last_accel_time = None

        # Drift correction
        self.velocity_history = deque(maxlen=10)
        # Stationary threshold: acceleration is magnitude after gravity is subtracted
        # So 0.20 m/s² represents small vibrations, not gravity
        self.stationary_threshold = 0.20  # m/s² (gravity-corrected motion magnitude)

        # Stationary tracking (for dynamic recalibration)
        self.is_stationary = False

        # Thread safety
        self.lock = threading.Lock()


    def update_gps(self, latitude, longitude, gps_speed=None, gps_accuracy=None):
        """Update with GPS data - thread safe"""
        with self.lock:
            current_time = time.time()

            # Calculate GPS-based velocity if we have previous position
            if self.last_gps_position and self.last_gps_time:
                dt = current_time - self.last_gps_time

                if dt > 0:
                    # Distance from last GPS position
                    dist = haversine_distance(
                        self.last_gps_position[0], self.last_gps_position[1],
                        latitude, longitude
                    )

                    # GPS velocity
                    gps_velocity = dist / dt

                    # Use provided GPS speed if available, otherwise calculated
                    if gps_speed is not None:
                        gps_velocity = gps_speed

                    # Use GPS accuracy as noise floor for distance accumulation
                    # This filters out GPS jitter while capturing real movement
                    if gps_accuracy is not None and gps_accuracy > 0:
                        # Subtract GPS noise floor to get true movement
                        true_movement = max(0.0, dist - gps_accuracy)
                        self.distance += true_movement
                    else:
                        # If no accuracy info or accuracy=0, assume 2.5m minimum floor
                        # (GPS providers may report 0 if unknown, use conservative default)
                        accuracy_floor = 2.5 if gps_accuracy == 0 else 0.0
                        true_movement = max(0.0, dist - accuracy_floor)
                        self.distance += true_movement

                    # STATIONARY DETECTION - Filter GPS noise (still used for velocity zeroing)
                    # Handle accuracy=0 as unknown accuracy (use conservative 5.0m floor)
                    if gps_accuracy is not None and gps_accuracy > 0:
                        movement_threshold = max(5.0, gps_accuracy * 1.5)
                    else:
                        movement_threshold = 5.0  # Default if accuracy unknown or zero
                    speed_threshold = 0.1  # m/s (~0.36 km/h) - optimized from testing

                    is_stationary = (dist < movement_threshold and gps_velocity < speed_threshold)
                    self.is_stationary = is_stationary  # Track for dynamic recalibration

                    if is_stationary:
                        # Stationary - zero out velocity
                        gps_velocity = 0.0
                        self.velocity = 0.0
                        self.accel_velocity = 0.0
                    else:
                        # Moving - fuse velocities
                        # Only fuse with accel if we've received actual accel data (not just init value 0.0)
                        if self.last_accel_time is not None:
                            self.velocity = (self.gps_weight * gps_velocity +
                                           self.accel_weight * self.accel_velocity)
                        else:
                            # No accel data yet, use GPS velocity as ground truth
                            self.velocity = gps_velocity

                        # Reset accelerometer velocity to GPS velocity (drift correction)
                        self.accel_velocity = self.velocity

            # Update GPS state
            self.last_gps_position = (latitude, longitude)
            self.last_gps_time = current_time
            self.last_gps_speed = gps_speed

            return self.velocity, self.distance

    def update_accelerometer(self, accel_magnitude):
        """Update with accelerometer data (forward acceleration in m/s²) - thread safe"""
        with self.lock:
            current_time = time.time()

            if self.last_accel_time is None:
                self.last_accel_time = current_time
                self.accel_magnitude = accel_magnitude
                return self.velocity, self.distance

            dt = current_time - self.last_accel_time

            if dt <= 0:
                return self.velocity, self.distance

            # Store current acceleration magnitude
            self.accel_magnitude = accel_magnitude

            # Integrate acceleration to get velocity
            if abs(accel_magnitude) < self.stationary_threshold:
                # Likely stationary, don't integrate
                accel_magnitude = 0

            # Update velocity
            self.accel_velocity += accel_magnitude * dt

            # Prevent negative velocity
            self.accel_velocity = max(0, self.accel_velocity)

            # NOTE: Distance is ONLY updated by GPS in update_gps() method (Haversine)
            # Removing accel-based distance updates prevents double-integration error
            # (was: self.distance += self.accel_velocity * dt)

            # If we don't have recent GPS, use accelerometer velocity
            if self.last_gps_time is None or (current_time - self.last_gps_time) > 5.0:
                self.velocity = self.accel_velocity

            self.last_accel_time = current_time

            return self.velocity, self.distance

    def reset(self):
        """Reset filter state - called after auto-save to prevent unbounded drift"""
        with self.lock:
            # Reset velocities and distance to prevent pure acceleration integration
            # from growing unbounded when GPS is unavailable
            self.velocity = 0.0
            self.accel_velocity = 0.0
            self.distance = 0.0  # Reset accumulated distance to prevent drift accumulation
            self.last_accel_time = None
            self.last_gps_time = None
            self.last_gps_position = None
            self.last_gps_speed = None
            self.velocity_history.clear()

    def get_state(self):
        """Get current state - thread safe"""
        with self.lock:
            return {
                'velocity': self.velocity,
                'distance': self.distance,
                'accel_velocity': self.accel_velocity,
                'accel_magnitude': self.accel_magnitude,
                'last_gps_time': self.last_gps_time,
                'is_stationary': self.is_stationary
            }

    def get_position(self):
        """
        Get current position as (latitude, longitude, uncertainty_m).

        For Complementary filter, returns last GPS position (no internal position tracking).
        Uncertainty is fixed at 5m (no uncertainty estimate in Complementary filter).

        Returns: (lat, lon, uncertainty_m)
        """
        with self.lock:
            if self.last_gps_position is None:
                return (0.0, 0.0, 999.9)  # No GPS lock yet

            lat, lon = self.last_gps_position
            uncertainty = 5.0  # Fixed uncertainty (Complementary has no uncertainty model)

            return lat, lon, uncertainty
