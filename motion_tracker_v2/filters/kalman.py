"""
Kalman filter for GPS + accelerometer sensor fusion.

Uses a constant acceleration model with 6D state vector:
[position_x, velocity_x, acceleration_x, position_y, velocity_y, acceleration_y]

GPS provides position measurements, accelerometer provides acceleration measurements.
The Kalman filter optimally fuses these measurements given their noise characteristics.
"""

import math
import threading
import time
import numpy as np
from .base import SensorFusionBase

# Try to import filterpy - required for Kalman filter
try:
    from filterpy.kalman import KalmanFilter as FilterPyKalmanFilter
    from filterpy.common import Q_discrete_white_noise
    HAS_FILTERPY = True
except ImportError:
    HAS_FILTERPY = False


class KalmanFilter(SensorFusionBase):
    """
    Kalman filter fusing GPS and accelerometer data.

    Uses constant acceleration state model:
    - State: [pos_x, vel_x, acc_x, pos_y, vel_y, acc_y]
    - Measurements: [gps_x, gps_y, accel_x, accel_y]

    Optimal fusion of GPS (accurate position, noisy) and accelerometer
    (noisy acceleration, high frequency) measurements.
    """

    def __init__(self, dt=0.02, gps_noise_std=5.0, accel_noise_std=0.5):
        """
        Initialize Kalman filter.

        Args:
            dt (float): Time step (seconds) - should match accel sample rate
            gps_noise_std (float): GPS position noise std dev (meters)
            accel_noise_std (float): Accel noise std dev (m/s²)

        Raises:
            ImportError: If filterpy is not available
        """
        if not HAS_FILTERPY:
            raise ImportError(
                "Kalman filter requires filterpy. Install with:\n"
                "pip install filterpy numpy"
            )

        # Initialize 6D Kalman filter
        # State: [pos_x, vel_x, acc_x, pos_y, vel_y, acc_y]
        self.kf = FilterPyKalmanFilter(dim_x=6, dim_z=4)
        self.dt = dt

        # State transition matrix (constant acceleration model)
        # x = x0 + v0*dt + 0.5*a*dt²
        # v = v0 + a*dt
        # a = a0 (constant)
        self.kf.F = np.array([
            [1, dt, 0.5*dt**2, 0,  0,         0        ],
            [0, 1,  dt,        0,  0,         0        ],
            [0, 0,  1,         0,  0,         0        ],
            [0, 0,  0,         1,  dt,        0.5*dt**2],
            [0, 0,  0,         0,  1,         dt       ],
            [0, 0,  0,         0,  0,         1        ]
        ])

        # Measurement matrix
        # We measure position (GPS) and acceleration (accelerometer)
        # z = [gps_x, gps_y, accel_x, accel_y]
        self.kf.H = np.array([
            [1, 0, 0, 0, 0, 0],  # Measure pos_x
            [0, 0, 0, 1, 0, 0],  # Measure pos_y
            [0, 0, 1, 0, 0, 0],  # Measure acc_x
            [0, 0, 0, 0, 0, 1]   # Measure acc_y
        ])

        # Measurement noise covariance
        self.kf.R = np.diag([
            gps_noise_std**2,      # GPS x
            gps_noise_std**2,      # GPS y
            accel_noise_std**2,    # Accel x
            accel_noise_std**2     # Accel y
        ])

        # Process noise (white noise on acceleration)
        q_std_accel = 0.1
        self.kf.Q = Q_discrete_white_noise(dim=3, dt=dt, var=q_std_accel**2, block_size=2)

        # Initial state covariance (high uncertainty)
        self.kf.P *= 1000

        # Initialize state to zero
        self.kf.x = np.zeros((6, 1))

        # Adapter state tracking
        self.last_gps_position = None  # (lat, lon) in degrees
        self.last_gps_lat_lon = None   # For haversine
        self.last_gps_time = None
        self.last_accel_time = None
        self.origin_lat_lon = None     # First GPS position (reference point)

        # Buffered measurements (for handling asynchronous updates)
        self.last_gps_xy = None        # [x, y] in meters from origin
        self.last_accel_xy = None      # [ax, ay] in m/s²

        # Output state
        self.distance = 0.0  # Total distance traveled (meters)
        self.velocity = 0.0  # Current velocity (m/s)
        self.is_stationary = False
        self.stationary_threshold = 0.1  # m/s

        # Thread safety
        self.lock = threading.Lock()

    def latlon_to_meters(self, lat, lon, origin_lat, origin_lon):
        """Convert lat/lon to local x/y meters from origin using equirectangular projection."""
        R = 6371000  # Earth radius in meters

        lat_rad = math.radians(lat)
        origin_lat_rad = math.radians(origin_lat)

        x = R * math.radians(lon - origin_lon) * math.cos(origin_lat_rad)
        y = R * math.radians(lat - origin_lat)

        return x, y

    def haversine_distance(self, lat1, lon1, lat2, lon2):
        """Calculate distance between two GPS coordinates in meters."""
        R = 6371000  # Earth radius in meters

        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        delta_phi = math.radians(lat2 - lat1)
        delta_lambda = math.radians(lon2 - lon1)

        a = (math.sin(delta_phi/2) ** 2 +
             math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda/2) ** 2)
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

        return R * c

    def update_gps(self, latitude, longitude, gps_speed=None, gps_accuracy=None):
        """Update Kalman filter with GPS measurement."""
        with self.lock:
            current_time = time.time()

            # Set origin on first GPS fix
            if self.origin_lat_lon is None:
                self.origin_lat_lon = (latitude, longitude)
                self.last_gps_lat_lon = (latitude, longitude)
                self.last_gps_time = current_time

                # Initialize Kalman state with first position
                x, y = self.latlon_to_meters(latitude, longitude, latitude, longitude)
                self.kf.x[0, 0] = x  # pos_x = 0 at origin
                self.kf.x[3, 0] = y  # pos_y = 0 at origin
                self.last_gps_xy = np.array([x, y])

                return self.velocity, self.distance

            # Convert lat/lon to local x/y meters
            x, y = self.latlon_to_meters(latitude, longitude,
                                         self.origin_lat_lon[0],
                                         self.origin_lat_lon[1])
            self.last_gps_xy = np.array([x, y])

            # Update distance traveled (haversine for accuracy)
            if self.last_gps_lat_lon is not None:
                dist_increment = self.haversine_distance(
                    self.last_gps_lat_lon[0], self.last_gps_lat_lon[1],
                    latitude, longitude
                )

                # Stationary detection
                movement_threshold = max(5.0, gps_accuracy * 1.5) if gps_accuracy else 5.0
                speed_threshold = 0.1  # m/s

                if gps_speed is not None:
                    is_stationary = (dist_increment < movement_threshold and gps_speed < speed_threshold)
                else:
                    is_stationary = (dist_increment < movement_threshold)

                self.is_stationary = is_stationary

                if not is_stationary:
                    self.distance += dist_increment

            # Predict step
            self.kf.predict()

            # Update step - combine GPS position with accelerometer (or defaults)
            accel_x = self.last_accel_xy[0] if self.last_accel_xy is not None else 0.0
            accel_y = self.last_accel_xy[1] if self.last_accel_xy is not None else 0.0

            # Always use 4D measurement vector: [gps_x, gps_y, accel_x, accel_y]
            z = np.array([
                [self.last_gps_xy[0]],
                [self.last_gps_xy[1]],
                [accel_x],
                [accel_y]
            ])
            self.kf.update(z)

            # Extract velocity from state (magnitude of velocity vector)
            vel_x = self.kf.x[1, 0]
            vel_y = self.kf.x[4, 0]
            self.velocity = math.sqrt(vel_x**2 + vel_y**2)

            # Zero out velocity if stationary
            if self.is_stationary:
                self.velocity = 0.0

            # Update tracking
            self.last_gps_lat_lon = (latitude, longitude)
            self.last_gps_time = current_time

            return self.velocity, self.distance

    def update_accelerometer(self, accel_magnitude):
        """Update Kalman filter with accelerometer measurement."""
        with self.lock:
            current_time = time.time()

            if self.last_accel_time is None:
                self.last_accel_time = current_time
                return self.velocity, self.distance

            dt = current_time - self.last_accel_time
            if dt <= 0:
                return self.velocity, self.distance

            # Estimate 2D acceleration from magnitude and velocity direction
            vel_x = self.kf.x[1, 0]
            vel_y = self.kf.x[4, 0]
            vel_mag = math.sqrt(vel_x**2 + vel_y**2)

            if vel_mag > 0.1:
                # Use velocity direction
                accel_x = accel_magnitude * (vel_x / vel_mag)
                accel_y = accel_magnitude * (vel_y / vel_mag)
            else:
                # Stationary or no clear direction - distribute equally
                accel_x = accel_magnitude * math.sqrt(0.5)
                accel_y = accel_magnitude * math.sqrt(0.5)

            self.last_accel_xy = np.array([accel_x, accel_y])

            # Predict step (time update)
            self.kf.predict()

            # Update step - combine accelerometer with GPS (or use default GPS)
            gps_x = self.last_gps_xy[0] if self.last_gps_xy is not None else 0.0
            gps_y = self.last_gps_xy[1] if self.last_gps_xy is not None else 0.0

            # Always use 4D measurement vector: [gps_x, gps_y, accel_x, accel_y]
            z = np.array([
                [gps_x],
                [gps_y],
                [self.last_accel_xy[0]],
                [self.last_accel_xy[1]]
            ])
            self.kf.update(z)

            # Extract velocity from state
            vel_x = self.kf.x[1, 0]
            vel_y = self.kf.x[4, 0]
            self.velocity = math.sqrt(vel_x**2 + vel_y**2)

            # Prevent negative velocity
            self.velocity = max(0, self.velocity)

            # Zero out if stationary
            if self.is_stationary:
                self.velocity = 0.0

            self.last_accel_time = current_time

            return self.velocity, self.distance

    def get_state(self):
        """Get current state in SensorFusion-compatible format."""
        with self.lock:
            # Extract acceleration from state for compatibility
            accel_x = self.kf.x[2, 0]
            accel_y = self.kf.x[5, 0]
            accel_magnitude = math.sqrt(accel_x**2 + accel_y**2)

            return {
                'velocity': self.velocity,
                'distance': self.distance,
                'accel_velocity': self.velocity,  # For compatibility
                'accel_magnitude': accel_magnitude,
                'last_gps_time': self.last_gps_time,
                'is_stationary': self.is_stationary
            }
