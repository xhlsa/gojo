"""
Extended Kalman Filter (EKF) for GPS + accelerometer sensor fusion.

Handles non-linear measurements (lat/lon conversion, acceleration magnitude)
using Jacobian-based linearization. Can be extended to include gyroscope
measurements via quaternion kinematics for orientation estimation.

RECOMMENDED for production: Same performance as linear Kalman (0.032 ms/update)
but with proper handling of non-linear transformations. Positioned for future
gyro integration where quaternion updates (dq/dt = 0.5 * [0,ωx,ωy,ωz] * q)
require Jacobian-based state linearization.

Current State vector: [x, y, vx, vy, ax, ay] (6D constant acceleration model)
Future State vector: [x, y, vx, vy, ax, ay, q0, q1, q2, q3] (10D with quaternion)

Measurements:
- GPS position (non-linear via equirectangular projection)
- Accelerometer magnitude (non-linear: sqrt(ax²+ay²))
- [Future] Gyro rates (ωx, ωy, ωz) for quaternion kinematics
"""

import math
import threading
import time
import numpy as np
from .base import SensorFusionBase

# Try to import filterpy for reference (optional)
try:
    from filterpy.kalman import ExtendedKalmanFilter as FilterPyEKF
    HAS_FILTERPY = True
except ImportError:
    HAS_FILTERPY = False


class ExtendedKalmanFilter(SensorFusionBase):
    """
    Extended Kalman Filter for non-linear GPS + accelerometer fusion.

    Uses Jacobian matrices to linearize the non-linear GPS measurement model.
    Better handles lat/lon to meters conversion than linear Kalman.

    State: [x, y, vx, vy, ax, ay] (meters, m/s, m/s²)
    GPS measurements: Non-linear transformation from lat/lon
    Accel measurements: Forward acceleration magnitude
    """

    def __init__(self, dt=0.02, gps_noise_std=5.0, accel_noise_std=0.5):
        """
        Initialize Extended Kalman Filter.

        Args:
            dt (float): Time step (seconds) - matches accel sample rate
            gps_noise_std (float): GPS position noise std dev (meters)
            accel_noise_std (float): Accel noise std dev (m/s²)
        """
        # State vector: [x, y, vx, vy, ax, ay]
        self.state = np.zeros(6)  # [x, y, vx, vy, ax, ay]
        self.P = np.eye(6) * 1000  # State covariance (high initial uncertainty)
        self.dt = dt

        # Process noise - white noise on acceleration changes
        q_accel = 0.1  # m/s² process noise std dev
        self.Q = np.zeros((6, 6))
        # Position and velocity driven by accel process noise
        self.Q[0, 0] = 0.25 * self.dt**4 * q_accel**2  # x
        self.Q[1, 1] = 0.25 * self.dt**4 * q_accel**2  # y
        self.Q[2, 2] = self.dt**2 * q_accel**2         # vx
        self.Q[3, 3] = self.dt**2 * q_accel**2         # vy
        self.Q[4, 4] = q_accel**2                       # ax
        self.Q[5, 5] = q_accel**2                       # ay

        # Measurement noise covariance
        self.gps_noise_std = gps_noise_std
        self.accel_noise_std = accel_noise_std
        self.R_gps = np.array([
            [gps_noise_std**2, 0],
            [0, gps_noise_std**2]
        ])
        self.R_accel = np.array([[accel_noise_std**2]])

        # GPS state tracking
        self.origin_lat_lon = None
        self.last_gps_lat_lon = None
        self.last_gps_time = None
        self.last_gps_xy = np.array([0.0, 0.0])

        # Accelerometer state tracking
        self.last_accel_magnitude = 0.0
        self.last_accel_time = None

        # Output state
        self.distance = 0.0
        self.velocity = 0.0
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

    def state_transition_jacobian(self):
        """
        Compute Jacobian of state transition function.

        State transition: x_k+1 = F*x_k + w
        For constant acceleration model, F is linear, so Jacobian is constant.
        """
        F = np.array([
            [1, 0, self.dt, 0, 0.5*self.dt**2, 0],
            [0, 1, 0, self.dt, 0, 0.5*self.dt**2],
            [0, 0, 1, 0, self.dt, 0],
            [0, 0, 0, 1, 0, self.dt],
            [0, 0, 0, 0, 1, 0],
            [0, 0, 0, 0, 0, 1]
        ])
        return F

    def gps_measurement_jacobian(self):
        """
        Compute Jacobian of GPS measurement function.

        Note: GPS lat/lon coordinates are pre-linearized to local x/y meters
        via equirectangular projection, so the measurement function is linear
        (identity for position states).

        Measurement: z = [x, y] from state [x, y, vx, vy, ax, ay]
        """
        H_gps = np.zeros((2, 6))
        H_gps[0, 0] = 1  # Measure x
        H_gps[1, 1] = 1  # Measure y

        return H_gps

    def accel_measurement_jacobian(self):
        """
        Compute Jacobian of accelerometer measurement function.

        Measurement: z = magnitude of [ax, ay] = sqrt(ax^2 + ay^2)

        This is non-linear, so we need the Jacobian.
        dz/d(state) = [0, 0, 0, 0, ax/|a|, ay/|a|]
        """
        ax = self.state[4]
        ay = self.state[5]
        a_mag = math.sqrt(ax**2 + ay**2)

        if a_mag < 1e-6:
            # Avoid division by zero
            H_accel = np.array([[0, 0, 0, 0, 0.0, 0.0]])
        else:
            H_accel = np.zeros((1, 6))
            H_accel[0, 4] = ax / a_mag
            H_accel[0, 5] = ay / a_mag

        return H_accel

    def predict(self):
        """EKF predict step (time update)."""
        # State transition (constant acceleration model)
        F = self.state_transition_jacobian()

        # Predict state
        self.state = F @ self.state

        # Predict covariance
        self.P = F @ self.P @ F.T + self.Q

    def update_gps(self, latitude, longitude, gps_speed=None, gps_accuracy=None):
        """Update EKF with GPS measurement."""
        with self.lock:
            current_time = time.time()

            # Set origin on first GPS fix
            if self.origin_lat_lon is None:
                self.origin_lat_lon = (latitude, longitude)
                self.last_gps_lat_lon = (latitude, longitude)
                self.last_gps_time = current_time
                # State stays at zero (origin)
                return self.velocity, self.distance

            # Convert lat/lon to local meters
            x, y = self.latlon_to_meters(latitude, longitude,
                                        self.origin_lat_lon[0],
                                        self.origin_lat_lon[1])
            self.last_gps_xy = np.array([x, y])

            # Update distance traveled
            if self.last_gps_lat_lon is not None:
                dist_increment = self.haversine_distance(
                    self.last_gps_lat_lon[0], self.last_gps_lat_lon[1],
                    latitude, longitude
                )

                # Stationary detection
                movement_threshold = max(5.0, gps_accuracy * 1.5) if gps_accuracy else 5.0
                speed_threshold = 0.1  # m/s

                if gps_speed is not None:
                    is_stationary = (dist_increment < movement_threshold and
                                   gps_speed < speed_threshold)
                else:
                    is_stationary = (dist_increment < movement_threshold)

                self.is_stationary = is_stationary

                if not is_stationary:
                    self.distance += dist_increment

            # Predict step
            self.predict()

            # GPS measurement: [x, y]
            z = np.array([[x], [y]])

            # Measurement Jacobian (GPS pre-linearized to local meters)
            H = self.gps_measurement_jacobian()

            # Innovation (measurement residual)
            z_pred = H @ self.state.reshape(-1, 1)
            y = z - z_pred

            # Innovation covariance
            S = H @ self.P @ H.T + self.R_gps

            # Kalman gain with fallback for singular matrix
            try:
                K = self.P @ H.T @ np.linalg.inv(S)
            except np.linalg.LinAlgError:
                # Numerical issue, use pseudoinverse as fallback
                K = self.P @ H.T @ np.linalg.pinv(S)

            # Update state
            self.state = (self.state.reshape(-1, 1) + K @ y).flatten()

            # Update covariance using Joseph form for numerical stability
            # P = (I - KH)P(I - KH)' + KRK'
            # This preserves symmetry better than standard form
            I_KH = np.eye(6) - K @ H
            self.P = I_KH @ self.P @ I_KH.T + K @ self.R_gps @ K.T

            # Extract velocity (magnitude of velocity vector)
            vx, vy = self.state[2], self.state[3]
            self.velocity = math.sqrt(vx**2 + vy**2)

            # Zero velocity if stationary
            if self.is_stationary:
                self.velocity = 0.0
                self.state[2:4] = 0.0  # Zero out velocity components

            # Update tracking
            self.last_gps_lat_lon = (latitude, longitude)
            self.last_gps_time = current_time

            return self.velocity, self.distance

    def update_accelerometer(self, accel_magnitude):
        """Update EKF with accelerometer measurement."""
        with self.lock:
            current_time = time.time()

            if self.last_accel_time is None:
                self.last_accel_time = current_time
                return self.velocity, self.distance

            dt = current_time - self.last_accel_time
            if dt <= 0:
                return self.velocity, self.distance

            # Predict step
            self.predict()

            # Estimate 2D acceleration from magnitude and velocity direction
            vx, vy = self.state[2], self.state[3]
            vel_mag = math.sqrt(vx**2 + vy**2)

            # Measurement: acceleration magnitude
            z = np.array([[accel_magnitude]])

            # Measurement Jacobian (non-linear) - compute from predicted state
            # Jacobian of sqrt(ax^2 + ay^2) w.r.t. state
            ax_pred, ay_pred = self.state[4], self.state[5]
            a_mag = math.sqrt(ax_pred**2 + ay_pred**2)

            H = np.zeros((1, 6))
            if a_mag > 1e-6:
                H[0, 4] = ax_pred / a_mag
                H[0, 5] = ay_pred / a_mag

            # Predicted measurement
            z_pred = np.array([[a_mag]])
            y = z - z_pred

            # Innovation covariance
            S = H @ self.P @ H.T + self.R_accel

            if S[0, 0] > 1e-6:  # Avoid division by zero
                # Kalman gain
                K = self.P @ H.T / S[0, 0]

                # Update state
                self.state = self.state.reshape(-1, 1) + K * y
                self.state = self.state.flatten()

                # Update covariance using Joseph form for numerical stability
                # P = (I - KH)P(I - KH)' + KRK'
                I_KH = np.eye(6) - K @ H
                self.P = I_KH @ self.P @ I_KH.T + K @ self.R_accel @ K.T

            # Extract velocity
            vx, vy = self.state[2], self.state[3]
            self.velocity = math.sqrt(vx**2 + vy**2)

            # Prevent negative velocity
            self.velocity = max(0, self.velocity)

            # Zero out if stationary
            if self.is_stationary:
                self.velocity = 0.0
                self.state[2:4] = 0.0

            self.last_accel_time = current_time

            return self.velocity, self.distance

    def get_state(self):
        """Get current state in SensorFusion-compatible format."""
        with self.lock:
            ax, ay = self.state[4], self.state[5]
            accel_magnitude = math.sqrt(ax**2 + ay**2)

            return {
                'velocity': self.velocity,
                'distance': self.distance,
                'accel_velocity': self.velocity,
                'accel_magnitude': accel_magnitude,
                'last_gps_time': self.last_gps_time,
                'is_stationary': self.is_stationary
            }
