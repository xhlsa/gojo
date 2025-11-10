#!/usr/bin/env python3
"""
Error-State EKF (ES-EKF) Filter for Trajectory Mapping During GPS Gaps

8D State Vector: [x, y, vx, vy, ax, ay, heading, heading_rate]
- Position (x, y) in local tangent plane (East-North)
- Velocity (vx, vy) with accel-based decomposition
- Heading (scalar yaw, radians) for dead reckoning during GPS gaps
- Heading rate (yaw rate) for turn prediction

Design: Simpler than 13D EKF (no quaternion), focuses on trajectory smoothing
vs orientation estimation. Fills GPS gaps via constant-velocity dead reckoning.

Key feature: Heading state enables curved trajectory prediction during GPS loss
vs naive constant-velocity which goes straight.
"""

import numpy as np
import threading
import time
import math
from collections import deque

# Try to use scipy for matrix operations, fallback to numpy
try:
    from scipy.linalg import inv, block_diag
except ImportError:
    inv = np.linalg.inv
    from numpy import block_diag


class ErrorStateEKF:
    """
    Error-State EKF for ground vehicle trajectory estimation.

    State: [x, y, vx, vy, ax, ay, heading, heading_rate]
    Dimensions: 8D (vs 13D quaternion EKF)

    Coordinate frame: Local Tangent Plane (East-North-Up)
    - x-axis: East (positive = eastward)
    - y-axis: North (positive = northward)
    - heading: 0 rad = East, π/2 = North (navigation convention)
    """

    def __init__(self, dt=0.02, gps_noise_std=8.0, accel_noise_std=0.5,
                 enable_gyro=False, gyro_noise_std=0.1):
        """
        Initialize ES-EKF filter.

        Args:
            dt: Time step (seconds), typically 0.02 (50 Hz)
            gps_noise_std: GPS position measurement noise (meters)
            accel_noise_std: Accelerometer magnitude measurement noise (m/s²)
            enable_gyro: Whether to use gyroscope for heading updates
            gyro_noise_std: Gyroscope measurement noise (rad/s)
        """
        self.dt = dt
        self.lock = threading.Lock()

        # State vector: [x, y, vx, vy, ax, ay, heading, heading_rate]
        self.state = np.zeros(8)

        # Covariance matrix (8x8)
        self.P = np.diag([100.0, 100.0, 10.0, 10.0, 1.0, 1.0, 0.1, 0.01])

        # Noise parameters
        self.gps_noise_std = gps_noise_std
        self.accel_noise_std = accel_noise_std
        self.enable_gyro = enable_gyro
        self.gyro_noise_std = gyro_noise_std

        # Process noise covariance (8x8 diagonal)
        # Higher during GPS gaps to reflect model uncertainty
        q_pos = 0.25 * dt**4 * accel_noise_std**2
        q_vel = dt**2 * accel_noise_std**2
        q_accel = 0.5  # m/s² process noise
        q_heading = 0.01  # rad² heading drift
        q_heading_rate = 0.005  # rad/s² heading rate drift

        self.Q = np.diag([q_pos, q_pos, q_vel, q_vel, q_accel, q_accel,
                          q_heading, q_heading_rate])

        # Measurement noise covariances
        self.R_gps = np.eye(2) * (gps_noise_std**2)
        self.R_accel = np.array([[accel_noise_std**2]])
        self.R_gyro = np.array([[gyro_noise_std**2]])

        # Origin for local coordinates (set on first GPS fix)
        self.origin_lat = None
        self.origin_lon = None
        self.origin_set = False

        # Track accumulated distance
        self.accumulated_distance = 0.0
        self.last_position = None

        # Gravity magnitude for accel processing
        self.gravity_magnitude = 9.81
        self.gravity_calibrated = False

        # GPS bearing for heading initialization
        self.last_gps_bearing = 0.0
        self.heading_initialized = False

        # Trajectory history for multi-track output
        self.trajectory = deque(maxlen=10000)

        # Statistics
        self.gps_update_count = 0
        self.accel_update_count = 0
        self.gyro_update_count = 0
        self.predict_count = 0

    def latlon_to_meters(self, lat, lon, origin_lat, origin_lon):
        """
        Convert latitude/longitude to local East-North meters using
        equirectangular projection (good for small areas <10km).

        Returns: (x_meters, y_meters)
        """
        R = 6371000.0  # Earth radius in meters

        # Differences
        d_lat = math.radians(lat - origin_lat)
        d_lon = math.radians(lon - origin_lon)

        # Equirectangular projection
        x = R * d_lon * math.cos(math.radians(origin_lat))  # East
        y = R * d_lat  # North

        return x, y

    def meters_to_latlon(self, x, y, origin_lat, origin_lon):
        """
        Convert local East-North meters back to latitude/longitude.

        Returns: (lat, lon)
        """
        R = 6371000.0  # Earth radius in meters

        d_lat = y / R
        d_lon = x / (R * math.cos(math.radians(origin_lat)))

        lat = origin_lat + math.degrees(d_lat)
        lon = origin_lon + math.degrees(d_lon)

        return lat, lon

    def haversine_distance(self, lat1, lon1, lat2, lon2):
        """
        Calculate great-circle distance between two points.

        Returns: distance in meters
        """
        R = 6371000.0  # Earth radius in meters

        d_lat = math.radians(lat2 - lat1)
        d_lon = math.radians(lon2 - lon1)

        a = math.sin(d_lat/2)**2 + math.cos(math.radians(lat1)) * \
            math.cos(math.radians(lat2)) * math.sin(d_lon/2)**2

        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

        return R * c

    def state_transition_jacobian(self):
        """
        Returns F matrix (8x8 state transition Jacobian).

        State evolution:
        x_{k+1} = x_k + vx_k*dt + 0.5*ax_k*dt²
        y_{k+1} = y_k + vy_k*dt + 0.5*ay_k*dt²
        vx_{k+1} = vx_k + ax_k*dt
        vy_{k+1} = vy_k + ay_k*dt
        ax_{k+1} = ax_k (constant accel assumption)
        ay_{k+1} = ay_k
        heading_{k+1} = heading_k + heading_rate_k*dt
        heading_rate_{k+1} = heading_rate_k (constant turn rate)
        """
        dt = self.dt
        dt2 = dt * dt

        F = np.array([
            [1, 0, dt, 0, 0.5*dt2, 0, 0, 0],
            [0, 1, 0, dt, 0, 0.5*dt2, 0, 0],
            [0, 0, 1, 0, dt, 0, 0, 0],
            [0, 0, 0, 1, 0, dt, 0, 0],
            [0, 0, 0, 0, 1, 0, 0, 0],
            [0, 0, 0, 0, 0, 1, 0, 0],
            [0, 0, 0, 0, 0, 0, 1, dt],
            [0, 0, 0, 0, 0, 0, 0, 1]
        ])

        return F

    def gps_measurement_jacobian(self):
        """
        Returns H_gps matrix (2x8) for GPS position measurement.

        Measurement model: h_gps = [x, y]
        Only measures position states directly.
        """
        H = np.zeros((2, 8))
        H[0, 0] = 1.0  # Measure x
        H[1, 1] = 1.0  # Measure y

        return H

    def accel_measurement_jacobian(self):
        """
        Returns H_accel matrix (1x8) for accelerometer magnitude measurement.

        Measurement model: h_accel = sqrt(ax² + ay²)
        Non-linear, so Jacobian uses current state values.
        """
        ax, ay = self.state[4], self.state[5]
        accel_mag = np.sqrt(ax**2 + ay**2) + 1e-6  # Avoid division by zero

        H = np.zeros((1, 8))
        H[0, 4] = ax / accel_mag  # ∂h/∂ax
        H[0, 5] = ay / accel_mag  # ∂h/∂ay

        return H

    def gyro_measurement_jacobian(self):
        """
        Returns H_gyro matrix (1x8) for gyroscope yaw rate measurement.

        Measurement model: h_gyro = heading_rate (only Z-axis)
        """
        H = np.zeros((1, 8))
        H[0, 7] = 1.0  # Measure heading_rate

        return H

    def predict(self):
        """
        Prediction step: evolve state and covariance.

        Key: Position prediction uses velocity decomposed by heading
        vx = |v| * cos(heading)
        vy = |v| * sin(heading)

        This enables curved trajectory prediction during GPS gaps.
        """
        with self.lock:
            # Get state
            x, y, vx, vy, ax, ay, heading, heading_rate = self.state

            # Velocity decomposition by heading (dead reckoning)
            vel_mag = np.sqrt(vx**2 + vy**2)
            vx_pred = vel_mag * np.cos(heading)
            vy_pred = vel_mag * np.sin(heading)

            # Standard motion model
            self.state[0] += vx_pred * self.dt + 0.5 * ax * self.dt**2
            self.state[1] += vy_pred * self.dt + 0.5 * ay * self.dt**2
            self.state[2] += ax * self.dt
            self.state[3] += ay * self.dt
            # Accel states unchanged (constant acceleration assumption)
            self.state[6] += heading_rate * self.dt
            # Heading rate unchanged

            # Covariance update: P = F*P*F^T + Q
            F = self.state_transition_jacobian()
            self.P = F @ self.P @ F.T + self.Q

            self.predict_count += 1

    def update_gps(self, latitude, longitude, gps_speed=None, gps_accuracy=None):
        """
        GPS position correction. Also initializes heading from GPS bearing
        on first fix.

        Returns: (velocity_magnitude, accumulated_distance)
        """
        with self.lock:
            # Set origin on first GPS fix
            if not self.origin_set:
                self.origin_lat = latitude
                self.origin_lon = longitude
                self.origin_set = True
                self.last_position = (latitude, longitude)
                self.state[0] = 0.0  # x = 0 at origin
                self.state[1] = 0.0  # y = 0 at origin
                self.gps_update_count += 1
                return 0.0, 0.0

            # Convert GPS to local coordinates
            x_meas, y_meas = self.latlon_to_meters(latitude, longitude,
                                                    self.origin_lat,
                                                    self.origin_lon)

            # GPS bearing for heading initialization (if speed > 0.5 m/s)
            if gps_speed and gps_speed > 0.5:
                # Bearing from previous fix to current
                lat_prev, lon_prev = self.last_position
                d_lat = math.radians(latitude - lat_prev)
                d_lon = math.radians(longitude - lon_prev)

                bearing = np.arctan2(
                    np.sin(d_lon) * np.cos(math.radians(latitude)),
                    np.cos(math.radians(lat_prev)) * np.sin(math.radians(latitude)) -
                    np.sin(math.radians(lat_prev)) * np.cos(math.radians(latitude)) * np.cos(d_lon)
                )
                self.last_gps_bearing = bearing

                # Initialize heading on first valid bearing
                if not self.heading_initialized:
                    self.state[6] = bearing
                    self.heading_initialized = True

            # Kalman update
            z = np.array([[x_meas], [y_meas]])
            H = self.gps_measurement_jacobian()

            # Measurement residual
            x_pred = self.state[[0, 1]].reshape(2, 1)
            y = z - H @ self.state.reshape(8, 1)

            # Innovation covariance
            # Adapt R based on GPS accuracy if provided
            R = self.R_gps.copy()
            if gps_accuracy:
                R = np.eye(2) * (gps_accuracy**2)

            S = H @ self.P @ H.T + R

            # Kalman gain
            K = self.P @ H.T @ inv(S)

            # State update
            dx = K @ y
            self.state = self.state.reshape(8, 1) + dx
            self.state = self.state.flatten()

            # Covariance update (Joseph form for stability)
            I_KH = np.eye(8) - K @ H
            self.P = I_KH @ self.P @ I_KH.T + K @ R @ K.T

            # Distance accumulation
            if self.last_position:
                lat_prev, lon_prev = self.last_position
                delta_dist = self.haversine_distance(
                    lat_prev, lon_prev, latitude, longitude
                )
                self.accumulated_distance += delta_dist

            self.last_position = (latitude, longitude)
            self.gps_update_count += 1

            # Return velocity magnitude and distance
            vel_mag = np.sqrt(self.state[2]**2 + self.state[3]**2)
            return vel_mag, self.accumulated_distance

    def update_accelerometer(self, accel_magnitude):
        """
        Accelerometer magnitude correction.

        After update, decompose velocity by heading:
        vx = |v| * cos(heading)
        vy = |v| * sin(heading)

        This enforces consistent heading-velocity relationship.

        Returns: (velocity_magnitude, accumulated_distance)
        """
        with self.lock:
            # Kalman update for accel magnitude
            z = np.array([[accel_magnitude]])
            H = self.accel_measurement_jacobian()

            # Prediction of measurement
            ax, ay = self.state[4], self.state[5]
            z_pred = np.sqrt(ax**2 + ay**2 + 1e-9).reshape(1, 1)

            # Innovation
            y = z - z_pred

            # Innovation covariance
            S = H @ self.P @ H.T + self.R_accel

            # Kalman gain
            K = self.P @ H.T @ inv(S + 1e-6 * np.eye(1))

            # State update
            dx = K @ y
            self.state = self.state.reshape(8, 1) + dx
            self.state = self.state.flatten()

            # Covariance update
            I_KH = np.eye(8) - K @ H
            self.P = I_KH @ self.P @ I_KH.T + K @ self.R_accel @ K.T

            # Decompose velocity by heading
            vel_mag = np.sqrt(self.state[2]**2 + self.state[3]**2)
            if not self.heading_initialized:
                # Use first velocity direction as initial heading
                if vel_mag > 0.1:
                    self.state[6] = np.arctan2(self.state[3], self.state[2])
                    self.heading_initialized = True
            else:
                # Enforce heading consistency
                self.state[2] = vel_mag * np.cos(self.state[6])
                self.state[3] = vel_mag * np.sin(self.state[6])

            self.accel_update_count += 1

            return vel_mag, self.accumulated_distance

    def update_gyroscope(self, gyro_x, gyro_y, gyro_z):
        """
        Gyroscope yaw rate correction.

        Only uses Z-axis (gyro_z) for heading rate update.
        Ignores X/Y (pitch/roll) as not needed for trajectory mapping.

        Returns: (velocity_magnitude, accumulated_distance)
        """
        if not self.enable_gyro:
            return np.sqrt(self.state[2]**2 + self.state[3]**2), self.accumulated_distance

        with self.lock:
            # Kalman update for yaw rate (gyro Z-axis only)
            z = np.array([[gyro_z]])
            H = self.gyro_measurement_jacobian()

            # Prediction
            z_pred = np.array([[self.state[7]]])

            # Innovation
            y = z - z_pred

            # Innovation covariance
            S = H @ self.P @ H.T + self.R_gyro

            # Kalman gain
            K = self.P @ H.T @ inv(S + 1e-6 * np.eye(1))

            # State update
            dx = K @ y
            self.state = self.state.reshape(8, 1) + dx
            self.state = self.state.flatten()

            # Covariance update
            I_KH = np.eye(8) - K @ H
            self.P = I_KH @ self.P @ I_KH.T + K @ self.R_gyro @ K.T

            self.gyro_update_count += 1

            vel_mag = np.sqrt(self.state[2]**2 + self.state[3]**2)
            return vel_mag, self.accumulated_distance

    def get_position(self):
        """
        Get current position as (latitude, longitude, uncertainty_m).

        Uncertainty calculated as average of diagonal covariance blocks.

        Returns: (lat, lon, uncertainty_m)
        """
        with self.lock:
            if not self.origin_set:
                return (0.0, 0.0, 999.9)

            # Current position in local frame
            x, y = self.state[0], self.state[1]

            # Convert back to lat/lon
            lat, lon = self.meters_to_latlon(x, y, self.origin_lat, self.origin_lon)

            # Position uncertainty (1-sigma radius)
            uncertainty = np.sqrt((self.P[0, 0] + self.P[1, 1]) / 2.0)

            return lat, lon, uncertainty

    def get_state(self):
        """
        Get complete state dictionary.

        Returns dict with position, velocity, distance, heading, uncertainty, etc.
        """
        with self.lock:
            lat, lon, uncertainty = self.get_position()

            vel_mag = np.sqrt(self.state[2]**2 + self.state[3]**2)
            accel_mag = np.sqrt(self.state[4]**2 + self.state[5]**2)
            heading_deg = np.degrees(self.state[6])
            heading_rate_degs = np.degrees(self.state[7])

            return {
                'position': (lat, lon),
                'position_local': (self.state[0], self.state[1]),
                'velocity': vel_mag,
                'velocity_vector': (self.state[2], self.state[3]),
                'acceleration': accel_mag,
                'acceleration_vector': (self.state[4], self.state[5]),
                'heading': self.state[6],
                'heading_deg': heading_deg,
                'heading_rate': self.state[7],
                'heading_rate_degs': heading_rate_degs,
                'distance': self.accumulated_distance,
                'uncertainty_m': uncertainty,
                'covariance_trace': np.trace(self.P),
                'gps_updates': self.gps_update_count,
                'accel_updates': self.accel_update_count,
                'gyro_updates': self.gyro_update_count
            }

    def reset(self):
        """
        Reset velocities and distance after auto-save (keep position).

        Called after each auto-save to flush accumulated data while
        maintaining position state for next segment.
        """
        with self.lock:
            # Keep position and heading, reset others
            # self.state[0:2] unchanged (position)
            self.state[2:4] = 0.0  # Reset velocity
            self.state[4:6] = 0.0  # Reset acceleration
            # Keep heading (6) and heading_rate (7)

            # Reset distance
            self.accumulated_distance = 0.0
