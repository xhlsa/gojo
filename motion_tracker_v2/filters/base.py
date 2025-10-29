"""
Abstract base class for sensor fusion filters.

All filter implementations must inherit from SensorFusionBase and implement
the required methods.
"""

from abc import ABC, abstractmethod


class SensorFusionBase(ABC):
    """
    Abstract base class for GPS + accelerometer sensor fusion filters.

    All subclasses must implement:
    - update_gps(latitude, longitude, gps_speed, gps_accuracy) -> (velocity, distance)
    - update_accelerometer(accel_magnitude) -> (velocity, distance)
    - get_state() -> dict

    This interface allows different fusion algorithms (complementary, Kalman, etc.)
    to be used interchangeably in the motion tracker.
    """

    @abstractmethod
    def update_gps(self, latitude, longitude, gps_speed=None, gps_accuracy=None):
        """
        Update filter with GPS measurement.

        Args:
            latitude (float): GPS latitude in degrees
            longitude (float): GPS longitude in degrees
            gps_speed (float, optional): GPS-reported speed in m/s
            gps_accuracy (float, optional): GPS accuracy in meters

        Returns:
            tuple: (velocity in m/s, distance traveled in meters)
        """
        pass

    @abstractmethod
    def update_accelerometer(self, accel_magnitude):
        """
        Update filter with accelerometer measurement.

        Args:
            accel_magnitude (float): Magnitude of forward acceleration in m/s²

        Returns:
            tuple: (velocity in m/s, distance traveled in meters)
        """
        pass

    @abstractmethod
    def get_state(self):
        """
        Get current filter state.

        Returns:
            dict: State dictionary with at least:
                - 'velocity': current velocity (m/s)
                - 'distance': total distance traveled (meters)
                - 'is_stationary': boolean indicating if device is stationary
                - 'last_gps_time': timestamp of last GPS update or None
        """
        pass
