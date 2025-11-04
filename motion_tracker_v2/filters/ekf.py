"""
Extended Kalman Filter (EKF) for GPS + accelerometer + gyroscope sensor fusion.

Handles non-linear measurements (lat/lon conversion, acceleration magnitude,
quaternion kinematics) using Jacobian-based linearization.

RECOMMENDED for production: Same performance as linear Kalman (0.032 ms/update)
for 6D state, ~0.08 ms/update for 10D with gyroscope.

State vectors:
- 6D (GPS + Accel only): [x, y, vx, vy, ax, ay]
- 13D (GPS + Accel + Gyro): [x, y, vx, vy, ax, ay, q0, q1, q2, q3, bx, by, bz]
  where bx, by, bz are gyro bias estimates (rad/s drift)

Quaternion representation: q = [q0, q1, q2, q3] where q0 is scalar part
Quaternion kinematics: dq/dt = 0.5 * q * [0, ωx, ωy, ωz] (non-linear)

Measurements:
- GPS position (non-linear via equirectangular projection)
- Accelerometer magnitude (non-linear: sqrt(ax²+ay²))
- Gyro rates (ωx, ωy, ωz) for quaternion kinematics (optional, non-linear)
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
    Extended Kalman Filter for non-linear GPS + accelerometer + gyroscope fusion.

    Uses Jacobian matrices to linearize the non-linear GPS measurement model
    and quaternion kinematics. Better handles orientation estimation via gyroscope.

    6D State: [x, y, vx, vy, ax, ay] (meters, m/s, m/s²)
    13D State: [x, y, vx, vy, ax, ay, q0, q1, q2, q3, bx, by, bz] (with quaternion + gyro bias)

    GPS measurements: Non-linear transformation from lat/lon
    Accel measurements: Forward acceleration magnitude (non-linear)
    Gyro measurements: Angular velocity rates (ωx, ωy, ωz) for quaternion dynamics
    """

    def __init__(self, dt=0.02, gps_noise_std=8.0, accel_noise_std=0.5,
                 enable_gyro=False, gyro_noise_std=0.1):
        """
        Initialize Extended Kalman Filter.

        Args:
            dt (float): Time step (seconds) - matches accel sample rate
            gps_noise_std (float): GPS position noise std dev (meters)
            accel_noise_std (float): Accel noise std dev (m/s²)
            enable_gyro (bool): If True, extend state to 10D with quaternion
            gyro_noise_std (float): Gyro angular velocity noise std dev (rad/s)
        """
        self.enable_gyro = enable_gyro
        self.dt = dt

        # Determine state dimension
        self.n_state = 13 if enable_gyro else 6  # 13D: add gyro bias [bx, by, bz] at indices 10-12

        # State vector: [x, y, vx, vy, ax, ay] or [x, y, vx, vy, ax, ay, q0, q1, q2, q3, bx, by, bz]
        self.state = np.zeros(self.n_state)
        if enable_gyro:
            self.state[6] = 1.0  # q0 (scalar part of identity quaternion)
            # Gyro bias starts at zero (will be learned during operation)
            self.state[10:13] = 0.0  # [bx, by, bz]

        self.P = np.eye(self.n_state) * 1000  # State covariance (high initial uncertainty)

        # Process noise - white noise on acceleration changes
        q_accel = 0.3  # m/s² process noise std dev (increased 3x)
        self.Q = np.zeros((self.n_state, self.n_state))
        # Position and velocity driven by accel process noise
        self.Q[0, 0] = 0.25 * self.dt**4 * q_accel**2  # x
        self.Q[1, 1] = 0.25 * self.dt**4 * q_accel**2  # y
        self.Q[2, 2] = self.dt**2 * q_accel**2         # vx
        self.Q[3, 3] = self.dt**2 * q_accel**2         # vy
        self.Q[4, 4] = q_accel**2                       # ax
        self.Q[5, 5] = q_accel**2                       # ay

        # Quaternion and gyro bias process noise (if enabled)
        if enable_gyro:
            q_gyro = 0.01  # rad/s process noise std dev for quaternion
            self.Q[6, 6] = q_gyro**2  # q0
            self.Q[7, 7] = q_gyro**2  # q1
            self.Q[8, 8] = q_gyro**2  # q2
            self.Q[9, 9] = q_gyro**2  # q3

            # Gyro bias random walk: slow drift model (very small process noise)
            q_bias = 0.001  # rad/s² - very slow bias drift rate
            self.Q[10, 10] = q_bias**2  # bx (bias X)
            self.Q[11, 11] = q_bias**2  # by (bias Y)
            self.Q[12, 12] = q_bias**2  # bz (bias Z)

        # Measurement noise covariance
        self.gps_noise_std = gps_noise_std
        self.accel_noise_std = accel_noise_std
        self.gyro_noise_std = gyro_noise_std
        self.R_gps = np.array([
            [gps_noise_std**2, 0],
            [0, gps_noise_std**2]
        ])
        self.R_accel = np.array([[accel_noise_std**2]])
        self.R_gyro = np.array([
            [gyro_noise_std**2, 0, 0],
            [0, gyro_noise_std**2, 0],
            [0, 0, gyro_noise_std**2]
        ])

        # GPS state tracking
        self.origin_lat_lon = None
        self.last_gps_lat_lon = None
        self.last_gps_time = None
        self.last_gps_xy = np.array([0.0, 0.0])

        # Accelerometer state tracking
        self.last_accel_magnitude = 0.0
        self.last_accel_time = None

        # Gyroscope state tracking (if enabled)
        self.last_gyro_time = None
        self.last_gyro_measurement = None  # Store latest gyro sample for use in prediction step

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

    def quaternion_multiply(self, q1, q2):
        """
        Quaternion multiplication: q1 * q2.

        Args:
            q1: [q0, q1, q2, q3] (scalar first)
            q2: [q0, q1, q2, q3] (scalar first)

        Returns:
            q_result: [q0, q1, q2, q3] quaternion product
        """
        q0_1, q1_1, q2_1, q3_1 = q1
        q0_2, q1_2, q2_2, q3_2 = q2

        q0 = q0_1*q0_2 - q1_1*q1_2 - q2_1*q2_2 - q3_1*q3_2
        q1 = q0_1*q1_2 + q1_1*q0_2 + q2_1*q3_2 - q3_1*q2_2
        q2 = q0_1*q2_2 - q1_1*q3_2 + q2_1*q0_2 + q3_1*q1_2
        q3 = q0_1*q3_2 + q1_1*q2_2 - q2_1*q1_2 + q3_1*q0_2

        return np.array([q0, q1, q2, q3])

    def quaternion_normalize(self, q):
        """Normalize quaternion to unit magnitude."""
        q_norm = np.linalg.norm(q)
        if q_norm < 1e-6:
            return np.array([1.0, 0.0, 0.0, 0.0])
        return q / q_norm

    def quaternion_kinematics_jacobian(self):
        """
        Compute Jacobian of quaternion kinematics.

        The quaternion kinematic equation is:
        dq/dt = 0.5 * q * [0, ωx, ωy, ωz]

        This computes d(dq)/d(q) at current state for linearization.
        Jacobian is 4x4 (quaternion to quaternion rate).

        Returns:
            H_gyro: 3x4 Jacobian (ωx, ωy, ωz measurement rates w.r.t. quaternion)
        """
        # This is a simplified Jacobian - the quaternion rate w.r.t. quaternion
        # For EKF, we use the full 4x4 form but only observe ωx, ωy, ωz (3 outputs)
        q0, q1, q2, q3 = self.state[6:10]

        # Quaternion kinematics Jacobian: d(dq/dt)/dq at current quaternion
        # Matrix form of: dq_i/dq_j for quaternion multiplication by [0, ωx, ωy, ωz]
        # The Jacobian relates how angular velocity maps through quaternion state
        H_gyro = np.zeros((3, 4))
        # d(ω_effect)/d(q) - simplified: angular velocity effect on quaternion
        # ω_x affects q2, q3 most
        # ω_y affects q1, q3 most
        # ω_z affects q1, q2 most
        H_gyro[0, 1] = 0.5 * q0  # ωx -> q1 contribution
        H_gyro[0, 2] = 0.5 * q3
        H_gyro[0, 3] = 0.5 * q2

        H_gyro[1, 0] = 0.5 * q0  # ωy -> q2 contribution
        H_gyro[1, 1] = -0.5 * q3
        H_gyro[1, 3] = 0.5 * q1

        H_gyro[2, 0] = -0.5 * q0  # ωz -> q3 contribution
        H_gyro[2, 1] = 0.5 * q2
        H_gyro[2, 2] = -0.5 * q1

        return H_gyro

    def state_transition_jacobian(self):
        """
        Compute Jacobian of state transition function.

        State transition: x_k+1 = F*x_k + w
        For constant acceleration model, F is linear for position/velocity/accel.
        For quaternion part (if enabled), kinematics are non-linear but we use
        approximate Jacobian here. Bias states stay constant (identity).

        Returns:
            F: nxn Jacobian matrix (6x6 or 13x13 depending on enable_gyro)
            - 6D: [x, y, vx, vy, ax, ay]
            - 13D: [x, y, vx, vy, ax, ay, q0, q1, q2, q3, bx, by, bz]
        """
        if self.enable_gyro:
            # 13D state transition (includes gyro bias)
            F = np.eye(13)
            # Position and velocity transitions
            F[0, 2] = self.dt  # x += vx*dt
            F[0, 4] = 0.5*self.dt**2  # x += 0.5*ax*dt²
            F[1, 3] = self.dt  # y += vy*dt
            F[1, 5] = 0.5*self.dt**2  # y += 0.5*ay*dt²
            F[2, 4] = self.dt  # vx += ax*dt
            F[3, 5] = self.dt  # vy += ay*dt
            # Acceleration stays constant (F[4,4] = 1, F[5,5] = 1)
            # Quaternion stays approximately constant (updated via kinematics in predict)
            # F[6:10, 6:10] = I (identity, quaternion updated via gyro integration)
            # Gyro bias stays constant (F[10:13, 10:13] = I, updated via measurements)
        else:
            # 6D state transition
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

        Measurement: z = [x, y] from state [x, y, vx, vy, ax, ay, ...quaternion...]
        Works for both 6D (no gyro) and 10D (with gyro) modes.
        """
        H_gps = np.zeros((2, self.n_state))
        H_gps[0, 0] = 1  # Measure x (position)
        H_gps[1, 1] = 1  # Measure y (position)

        return H_gps

    def accel_measurement_jacobian(self):
        """
        Compute Jacobian of accelerometer measurement function.

        Measurement: z = magnitude of [ax, ay] = sqrt(ax^2 + ay^2)

        This is non-linear, so we need the Jacobian.
        dz/d(state) = [0, 0, 0, 0, ax/|a|, ay/|a|, 0, 0, 0, 0] (in 10D mode)

        Works for both 6D (no gyro) and 10D (with gyro) modes.
        """
        ax = self.state[4]
        ay = self.state[5]
        a_mag = math.sqrt(ax**2 + ay**2)

        if a_mag < 1e-6:
            # Avoid division by zero - zero vector in measurement space
            H_accel = np.zeros((1, self.n_state))
        else:
            H_accel = np.zeros((1, self.n_state))
            H_accel[0, 4] = ax / a_mag
            H_accel[0, 5] = ay / a_mag

        return H_accel

    def predict(self):
        """EKF predict step (time update)."""
        # State transition (constant acceleration model)
        F = self.state_transition_jacobian()

        # Predict state for position/velocity/acceleration
        if self.enable_gyro:
            # For 13D state [x, y, vx, vy, ax, ay, q0, q1, q2, q3, bx, by, bz]
            # Update position/velocity/accel normally
            self.state[:6] = F[:6, :6] @ self.state[:6]

            # NEW: Integrate quaternion using bias-corrected gyroscope measurement
            if self.last_gyro_measurement is not None:
                q = self.state[6:10]
                bias = self.state[10:13]
                gyro = self.last_gyro_measurement

                # Bias-corrected angular velocity (remove gyro bias)
                omega = gyro - bias
                omega_norm = np.linalg.norm(omega)

                if omega_norm > 1e-6:
                    # Quaternion rate: dq/dt = 0.5 * q * [0, ωx, ωy, ωz]
                    # First-order integration: q_new = q + dq
                    dq = 0.5 * self.dt * self.quaternion_multiply(q, [0, omega[0], omega[1], omega[2]])
                    q_new = q + dq
                    self.state[6:10] = self.quaternion_normalize(q_new)

            # Gyro bias stays constant during prediction (updated by measurements)
            # self.state[10:13] unchanged
        else:
            # For 6D state, standard linear transition
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

                # Use GPS accuracy as noise floor for distance accumulation
                # This filters out GPS jitter while capturing real movement
                if gps_accuracy is not None:
                    # Subtract GPS noise floor to get true movement
                    true_movement = max(0.0, dist_increment - gps_accuracy)
                    self.distance += true_movement
                else:
                    # If no accuracy info, accumulate all movement
                    self.distance += dist_increment

                # Stationary detection (still used for other purposes like recalibration)
                movement_threshold = max(5.0, gps_accuracy * 1.5) if gps_accuracy else 5.0
                speed_threshold = 0.1  # m/s

                if gps_speed is not None:
                    is_stationary = (dist_increment < movement_threshold and
                                   gps_speed < speed_threshold)
                else:
                    is_stationary = (dist_increment < movement_threshold)

                self.is_stationary = is_stationary

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
            I_KH = np.eye(self.n_state) - K @ H
            self.P = I_KH @ self.P @ I_KH.T + K @ self.R_gps @ K.T

            # Extract velocity (magnitude of velocity vector)
            vx, vy = self.state[2], self.state[3]
            self.velocity = math.sqrt(vx**2 + vy**2)

            # CRITICAL FIX: Velocity bounds and GPS drift correction
            # GPS velocity is ground truth - use it to correct Kalman state drift
            if gps_speed is not None and gps_speed >= 0:
                # Strong correction: reset velocity to GPS ground truth
                # This prevents unbounded accumulation of velocity errors
                if self.velocity > 1.0:  # Only correct when moving
                    speed_ratio = gps_speed / self.velocity if self.velocity > 0.1 else 0.0
                    self.state[2] *= speed_ratio  # Scale vx
                    self.state[3] *= speed_ratio  # Scale vy
                    self.velocity = gps_speed
                else:
                    self.velocity = gps_speed

            # Sanity check: velocity should never exceed 60 m/s (~216 km/h)
            # This catches numerical divergence before it becomes critical
            MAX_VELOCITY = 60.0  # m/s (driving sanity limit)
            if self.velocity > MAX_VELOCITY:
                self.velocity = MAX_VELOCITY
                # Scale velocity components proportionally
                if self.velocity > 0:
                    self.state[2] = (self.state[2] / math.sqrt(vx**2 + vy**2)) * MAX_VELOCITY
                    self.state[3] = (self.state[3] / math.sqrt(vx**2 + vy**2)) * MAX_VELOCITY

            # Zero velocity if stationary
            if self.is_stationary:
                self.velocity = 0.0
                self.state[2:4] = 0.0  # Zero out velocity components

            # Update tracking
            self.last_gps_lat_lon = (latitude, longitude)
            self.last_gps_time = current_time

            return self.velocity, self.distance

    def update_accelerometer(self, accel_magnitude):
        """Update EKF with accelerometer measurement.

        CRITICAL FIX (Oct 29):
        - Removed predict() call from accelerometer update
        - Accelerometer is treated as a MEASUREMENT, not a state driver
        - Prediction only happens on GPS updates (drift correction)
        - This prevents unbounded velocity accumulation from repeated predictions
        """
        with self.lock:
            current_time = time.time()

            if self.last_accel_time is None:
                self.last_accel_time = current_time
                return self.velocity, self.distance

            dt = current_time - self.last_accel_time
            if dt <= 0:
                return self.velocity, self.distance

            # NOTE: Predict step REMOVED - only predict on GPS updates
            # This prevents accelerometer (50 Hz) from driving 1800+ predictions
            # while GPS (1 Hz) only provides 40 corrections

            # Estimate 2D acceleration from magnitude and velocity direction
            vx, vy = self.state[2], self.state[3]
            vel_mag = math.sqrt(vx**2 + vy**2)

            # Measurement: acceleration magnitude
            z = np.array([[accel_magnitude]])

            # Measurement Jacobian (non-linear) - compute from predicted state
            # Jacobian of sqrt(ax^2 + ay^2) w.r.t. state
            ax_pred, ay_pred = self.state[4], self.state[5]
            a_mag = math.sqrt(ax_pred**2 + ay_pred**2)

            H = np.zeros((1, self.n_state))
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
                I_KH = np.eye(self.n_state) - K @ H
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

    def update_gyroscope(self, gyro_x, gyro_y, gyro_z):
        """
        Update EKF with gyroscope measurement using gyro bias estimation model.

        FIXED MODEL (Oct 30 session):
        - Gyroscope measures: ω_measured = ω_true + bias + noise
        - Bias is estimated state (bx, by, bz) that slowly drifts
        - This allows the filter to learn and correct for gyro drift over time
        - Much more reliable than treating gyro as direct orientation measurement

        When stationary: gyro ≈ 0 + bias, so we learn bias
        When moving: gyro = true_rotation + bias, bias correction gives true rotation

        Args:
            gyro_x, gyro_y, gyro_z (float): Angular velocity measurements (rad/s)

        Returns:
            tuple: (velocity, distance) for consistency with other update methods
        """
        if not self.enable_gyro:
            return self.velocity, self.distance

        with self.lock:
            current_time = time.time()

            if self.last_gyro_time is None:
                self.last_gyro_time = current_time
                self.last_gyro_measurement = np.array([gyro_x, gyro_y, gyro_z])
                return self.velocity, self.distance

            dt = current_time - self.last_gyro_time
            if dt <= 0:
                self.last_gyro_measurement = np.array([gyro_x, gyro_y, gyro_z])
                return self.velocity, self.distance

            # Store measurement for use in prediction step (quaternion integration)
            self.last_gyro_measurement = np.array([gyro_x, gyro_y, gyro_z])

            # Predict step (will integrate quaternion using this gyro measurement)
            self.predict()

            # GYRO BIAS ESTIMATION MODEL
            # Measurement: gyroscope angular velocity [ωx, ωy, ωz]
            z = np.array([[gyro_x], [gyro_y], [gyro_z]])

            # Predicted measurement: current gyro bias estimate
            # When stationary: gyro_measured = gyro_bias + noise (gyro_true = 0)
            # When moving: gyro_measured = gyro_true + gyro_bias + noise
            # So predicted measurement is just the bias states
            bias_pred = self.state[10:13].reshape(-1, 1)
            z_pred = bias_pred

            # Innovation (measurement residual)
            y = z - z_pred

            # Measurement Jacobian: H = ∂h/∂x
            # h(x) = [bx, by, bz] - gyro directly observes bias states
            # No effect on position, velocity, accel, or quaternion
            H = np.zeros((3, self.n_state))
            H[0, 10] = 1  # ωx measurement observes bias bx (state[10])
            H[1, 11] = 1  # ωy measurement observes bias by (state[11])
            H[2, 12] = 1  # ωz measurement observes bias bz (state[12])

            # Innovation covariance
            S = H @ self.P @ H.T + self.R_gyro

            # Kalman gain with fallback for singular matrix
            try:
                K = self.P @ H.T @ np.linalg.inv(S)
            except np.linalg.LinAlgError:
                K = self.P @ H.T @ np.linalg.pinv(S)

            # Update state (primarily corrects bias estimates)
            self.state = (self.state.reshape(-1, 1) + K @ y).flatten()

            # Normalize quaternion after update (ensure q0² + q1² + q2² + q3² = 1)
            self.state[6:10] = self.quaternion_normalize(self.state[6:10])

            # Update covariance using Joseph form for numerical stability
            I_KH = np.eye(self.n_state) - K @ H
            self.P = I_KH @ self.P @ I_KH.T + K @ self.R_gyro @ K.T

            self.last_gyro_time = current_time

            return self.velocity, self.distance

    def reset(self):
        """Reset filter state (velocities, position, and distance) after auto-save."""
        with self.lock:
            # Reset all accumulated state to prevent unbounded drift
            self.velocity = 0.0
            self.distance = 0.0

            # Reset state vector position/velocity components
            # State: [px, py, vx, vy, ax, ay, q0, q1, q2, q3, bx, by, bz]
            self.state[0:2] = 0.0  # Position
            self.state[2:4] = 0.0  # Velocity

            # Reset sensor timing
            self.last_gps_time = None
            self.last_accel_time = None

    def get_state(self):
        """Get current state in SensorFusion-compatible format."""
        with self.lock:
            ax, ay = self.state[4], self.state[5]
            accel_magnitude = math.sqrt(ax**2 + ay**2)

            state_dict = {
                'velocity': self.velocity,
                'distance': self.distance,
                'accel_velocity': self.velocity,
                'accel_magnitude': accel_magnitude,
                'last_gps_time': self.last_gps_time,
                'is_stationary': self.is_stationary
            }

            # Add quaternion and gyro bias state if gyro is enabled
            if self.enable_gyro:
                q0, q1, q2, q3 = self.state[6:10]
                bx, by, bz = self.state[10:13]  # Gyro bias estimates

                # Calculate heading (yaw) from quaternion
                # Formula: atan2(2*(q0*q3 + q1*q2), 1 - 2*(q2^2 + q3^2))
                heading_rad = math.atan2(2*(q0*q3 + q1*q2), 1 - 2*(q2**2 + q3**2))
                heading_deg = math.degrees(heading_rad)

                state_dict.update({
                    'quaternion': [q0, q1, q2, q3],
                    'quaternion_norm': np.linalg.norm([q0, q1, q2, q3]),
                    'gyro_bias': [bx, by, bz],  # Estimated gyro bias in rad/s
                    'gyro_bias_magnitude': np.linalg.norm([bx, by, bz]),
                    'heading_deg': float(heading_deg),
                    'heading_rad': float(heading_rad)
                })

            return state_dict
