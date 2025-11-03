"""
Unscented Kalman Filter (UKF) for GPS + accelerometer sensor fusion.

Uses sigma points to handle non-linear transformations without Jacobians.
Superior to EKF for highly non-linear systems and better numerical stability.

State vector: [x, y, vx, vy, ax, ay] (6D constant acceleration model)
Advantages over EKF:
- No Jacobian computation needed
- Better approximation of non-linear functions (2nd order accurate)
- More stable for GPS coordinate transformations
- Handles discontinuities better
"""

import math
import threading
import time
import numpy as np
from .base import SensorFusionBase


class UnscentedKalmanFilter(SensorFusionBase):
    """
    Unscented Kalman Filter for GPS + accelerometer fusion.

    Uses sigma points (scaled deterministic points around mean) to handle
    non-linear transformations without requiring Jacobians.

    State: [x, y, vx, vy, ax, ay] (meters, m/s, m/s²)
    GPS measurements: Non-linear lat/lon transformation
    Accel measurements: Forward acceleration magnitude
    """

    def __init__(self, dt=0.02, gps_noise_std=5.0, accel_noise_std=0.5,
                 alpha=1e-3, beta=2.0, kappa=0.0):
        """
        Initialize Unscented Kalman Filter.

        Args:
            dt (float): Time step (seconds)
            gps_noise_std (float): GPS position noise std dev (meters)
            accel_noise_std (float): Accel noise std dev (m/s²)
            alpha (float): Spread of sigma points around mean (default: 1e-3)
                          Smaller alpha keeps points closer to mean (more conservative)
            beta (float): Used to incorporate prior knowledge of distribution.
                         For Gaussian, optimal is 2.0
            kappa (float): Secondary scaling parameter (default: 0.0)
                          Often set to 0 or 3 - n_states
        """
        self.dt = dt
        self.n = 6  # State dimension

        # UKF parameters
        self.alpha = alpha
        self.beta = beta
        self.kappa = kappa
        self.lambda_ = alpha**2 * (self.n + kappa) - self.n
        self.gamma = math.sqrt(self.n + self.lambda_)

        # Sigma point weights
        self.Wm = np.zeros(2*self.n + 1)  # Weights for mean
        self.Wc = np.zeros(2*self.n + 1)  # Weights for covariance

        self.Wm[0] = self.lambda_ / (self.n + self.lambda_)
        self.Wc[0] = self.lambda_ / (self.n + self.lambda_) + (1 - alpha**2 + beta)

        for i in range(1, 2*self.n + 1):
            self.Wm[i] = 1 / (2 * (self.n + self.lambda_))
            self.Wc[i] = 1 / (2 * (self.n + self.lambda_))

        # State and covariance
        self.state = np.zeros(self.n)
        self.P = np.eye(self.n) * 1000  # High initial uncertainty

        # Process noise covariance
        q_accel = 0.1
        self.Q = np.zeros((self.n, self.n))
        self.Q[0, 0] = 0.25 * dt**4 * q_accel**2
        self.Q[1, 1] = 0.25 * dt**4 * q_accel**2
        self.Q[2, 2] = dt**2 * q_accel**2
        self.Q[3, 3] = dt**2 * q_accel**2
        self.Q[4, 4] = q_accel**2
        self.Q[5, 5] = q_accel**2

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
        self.stationary_threshold = 0.1

        # Thread safety
        self.lock = threading.Lock()

    def generate_sigma_points(self, x, P):
        """
        Generate 2*n+1 sigma points from state and covariance.

        Sigma points are spread around the mean to capture non-linear
        transformations better than linear approximations.
        """
        sigma_points = np.zeros((2*self.n + 1, self.n))
        sigma_points[0] = x

        # Compute sqrt of covariance
        try:
            L = np.linalg.cholesky(P)
        except np.linalg.LinAlgError:
            # Fallback: add small regularization if not positive definite
            L = np.linalg.cholesky(P + np.eye(self.n) * 1e-6)

        sqrt_term = self.gamma * L

        # Generate sigma points around mean
        for i in range(self.n):
            sigma_points[i + 1] = x + sqrt_term[:, i]
            sigma_points[i + self.n + 1] = x - sqrt_term[:, i]

        return sigma_points

    def latlon_to_meters(self, lat, lon, origin_lat, origin_lon):
        """Convert lat/lon to local x/y meters from origin."""
        R = 6371000

        lat_rad = math.radians(lat)
        origin_lat_rad = math.radians(origin_lat)

        x = R * math.radians(lon - origin_lon) * math.cos(origin_lat_rad)
        y = R * math.radians(lat - origin_lat)

        return x, y

    def haversine_distance(self, lat1, lon1, lat2, lon2):
        """Calculate distance between two GPS coordinates."""
        R = 6371000

        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        delta_phi = math.radians(lat2 - lat1)
        delta_lambda = math.radians(lon2 - lon1)

        a = (math.sin(delta_phi/2) ** 2 +
             math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda/2) ** 2)
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

        return R * c

    def state_transition(self, x):
        """
        Non-linear state transition function (constant acceleration model).

        x_k+1 = F * x_k (actually linear in this case, but kept for clarity)
        """
        x_new = np.zeros_like(x)
        x_new[0] = x[0] + x[2]*self.dt + 0.5*x[4]*self.dt**2  # x
        x_new[1] = x[1] + x[3]*self.dt + 0.5*x[5]*self.dt**2  # y
        x_new[2] = x[2] + x[4]*self.dt                         # vx
        x_new[3] = x[3] + x[5]*self.dt                         # vy
        x_new[4] = x[4]                                        # ax
        x_new[5] = x[5]                                        # ay
        return x_new

    def predict(self):
        """UKF predict step."""
        # Generate sigma points
        sigma_pts = self.generate_sigma_points(self.state, self.P)

        # Propagate sigma points through state transition
        sigma_pts_pred = np.array([self.state_transition(s) for s in sigma_pts])

        # Compute predicted state (weighted mean)
        self.state = np.sum(self.Wm[:, np.newaxis] * sigma_pts_pred, axis=0)

        # Compute predicted covariance
        self.P = np.zeros((self.n, self.n))
        for i in range(2*self.n + 1):
            diff = sigma_pts_pred[i] - self.state
            self.P += self.Wc[i] * np.outer(diff, diff)
        self.P += self.Q

    def gps_measurement_transform(self, sigma_pts):
        """Transform sigma points to GPS measurement space (x, y)."""
        # For GPS, we measure position directly (no transformation needed)
        # Just extract x, y from state
        measurements = np.zeros((2*self.n + 1, 2))
        measurements[:, 0] = sigma_pts[:, 0]  # x
        measurements[:, 1] = sigma_pts[:, 1]  # y
        return measurements

    def accel_measurement_transform(self, sigma_pts):
        """Transform sigma points to acceleration magnitude."""
        measurements = np.zeros(2*self.n + 1)
        for i in range(2*self.n + 1):
            ax, ay = sigma_pts[i, 4], sigma_pts[i, 5]
            measurements[i] = math.sqrt(ax**2 + ay**2)
        return measurements

    def update_gps(self, latitude, longitude, gps_speed=None, gps_accuracy=None):
        """Update UKF with GPS measurement."""
        with self.lock:
            current_time = time.time()

            # Set origin on first GPS fix
            if self.origin_lat_lon is None:
                self.origin_lat_lon = (latitude, longitude)
                self.last_gps_lat_lon = (latitude, longitude)
                self.last_gps_time = current_time
                return self.velocity, self.distance

            # Convert to meters
            x, y = self.latlon_to_meters(latitude, longitude,
                                        self.origin_lat_lon[0],
                                        self.origin_lat_lon[1])
            self.last_gps_xy = np.array([x, y])

            # Update distance
            if self.last_gps_lat_lon is not None:
                dist_increment = self.haversine_distance(
                    self.last_gps_lat_lon[0], self.last_gps_lat_lon[1],
                    latitude, longitude
                )

                # Use GPS accuracy as noise floor for distance accumulation
                # This filters out GPS jitter while capturing real movement
                if gps_accuracy is not None:
                    # Subtract GPS noise floor to get true movement
                    true_movement = max(0.0, dist_increment - gps_accuracy)
                    self.distance += true_movement
                else:
                    # If no accuracy info, accumulate all movement
                    self.distance += dist_increment

                # Stationary detection (still used for velocity zeroing)
                movement_threshold = max(5.0, gps_accuracy * 1.5) if gps_accuracy else 5.0
                speed_threshold = 0.1

                if gps_speed is not None:
                    is_stationary = (dist_increment < movement_threshold and
                                   gps_speed < speed_threshold)
                else:
                    is_stationary = (dist_increment < movement_threshold)

                self.is_stationary = is_stationary

            # Predict step
            self.predict()

            # Generate sigma points for update
            sigma_pts = self.generate_sigma_points(self.state, self.P)

            # Transform to measurement space
            z_sigma = self.gps_measurement_transform(sigma_pts)

            # Compute measurement mean and covariance
            z_mean = np.sum(self.Wm[:, np.newaxis] * z_sigma, axis=0)

            Pz = np.zeros((2, 2))
            for i in range(2*self.n + 1):
                diff = z_sigma[i] - z_mean
                Pz += self.Wc[i] * np.outer(diff, diff)
            Pz += self.R_gps

            # Cross-covariance
            Pxz = np.zeros((self.n, 2))
            for i in range(2*self.n + 1):
                state_diff = sigma_pts[i] - self.state
                meas_diff = z_sigma[i] - z_mean
                Pxz += self.Wc[i] * np.outer(state_diff, meas_diff)

            # Kalman gain
            try:
                K = Pxz @ np.linalg.inv(Pz)
            except np.linalg.LinAlgError:
                # Numerical issue, use pseudoinverse as fallback
                K = Pxz @ np.linalg.pinv(Pz)

            # Measurement residual
            z = np.array([x, y])
            residual = z - z_mean

            # Update state and covariance using standard UKF update
            # (UKF is already numerically stable without Joseph form)
            self.state = self.state + K @ residual
            self.P = self.P - K @ Pz @ K.T

            # Extract velocity
            vx, vy = self.state[2], self.state[3]
            self.velocity = math.sqrt(vx**2 + vy**2)

            if self.is_stationary:
                self.velocity = 0.0
                self.state[2:4] = 0.0

            self.last_gps_lat_lon = (latitude, longitude)
            self.last_gps_time = current_time

            return self.velocity, self.distance

    def update_accelerometer(self, accel_magnitude):
        """Update UKF with accelerometer measurement."""
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

            # Decompose magnitude into 2D
            vx, vy = self.state[2], self.state[3]
            vel_mag = math.sqrt(vx**2 + vy**2)

            # Generate sigma points for prediction and update
            sigma_pts = self.generate_sigma_points(self.state, self.P)

            # Transform to measurement space (acceleration magnitude)
            z_sigma = self.accel_measurement_transform(sigma_pts)

            # Compute measurement mean and covariance
            z_mean = np.sum(self.Wm * z_sigma)

            Pz = 0.0
            for i in range(2*self.n + 1):
                diff = z_sigma[i] - z_mean
                Pz += self.Wc[i] * diff**2
            Pz += self.R_accel[0, 0]

            # Cross-covariance
            Pxz = np.zeros(self.n)
            for i in range(2*self.n + 1):
                state_diff = sigma_pts[i] - self.state
                meas_diff = z_sigma[i] - z_mean
                Pxz += self.Wc[i] * state_diff * meas_diff

            # Kalman gain and update
            if Pz > 1e-6:
                K = Pxz / Pz

                # Measurement residual
                residual = accel_magnitude - z_mean

                # Update state and covariance using standard UKF update
                # (UKF is already numerically stable without Joseph form)
                self.state = self.state + K * residual
                self.P = self.P - np.outer(K, K) * Pz

            # Extract velocity
            vx, vy = self.state[2], self.state[3]
            self.velocity = math.sqrt(vx**2 + vy**2)
            self.velocity = max(0, self.velocity)

            if self.is_stationary:
                self.velocity = 0.0
                self.state[2:4] = 0.0

            self.last_accel_time = current_time

            return self.velocity, self.distance

    def reset(self):
        """Reset filter state (velocities, position, and distance) after auto-save."""
        with self.lock:
            self.velocity = 0.0
            self.distance = 0.0
            self.state[0:2] = 0.0  # px, py (position)
            self.state[2:4] = 0.0  # vx, vy (velocity)
            self.last_gps_time = None
            self.last_accel_time = None

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
